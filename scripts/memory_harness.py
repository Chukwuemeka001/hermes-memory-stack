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

  * TIER 2 (optional, gated, OFF by default): a model-backed grader that asks a model
    the task under the FULL vs the PROJECTED memory block and scores actual answer
    quality (does the projected answer still preserve the gold-required facts and
    honour the pinned constraints?). Behind ``--tier2``: the ``null`` grader (default,
    DISABLED no-op, never a model call), a ``fixture`` grader (replays canned verdicts
    for tests / no-spend smoke), and ``claude-cli`` (the real grader — the direct
    Claude Code CLI on subscription auth, NOT the Anthropic API; API-key env vars are
    stripped so it can neither bill nor leak a key). An unreachable model is BLOCKED,
    never a pass. The default path never instantiates a model grader, never spawns a
    subprocess, and never imports a model — so the default tests need no API key.

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
    # Tier 2 (opt-in, model-backed answer-quality grade):
    python3 scripts/memory_harness.py --tier2 --tier2-grader fixture \
            --tier2-fixture verdicts.json                     # no-spend wiring smoke
    python3 scripts/memory_harness.py --tier2 --tier2-grader claude-cli \
            --tier2-task safety-leaked-api-key --json         # real grader, one task

Exit code: 0 if no task FAILs (WARN allowed), 1 if any FAIL (Tier 1 or Tier 2, or any
WARN under --strict), 2 on a usage/fixture-load error, 3 if a Tier-2 grader was
requested but BLOCKED/unreachable (loud — never silently a pass).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import shutil
import subprocess
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

# --- Tier 2 (optional, model-backed answer-quality grading) ------------------ #
# Distinct from Tier 1's PASS/WARN/FAIL: a Tier-2 task can also be BLOCKED (the
# grader/model was unreachable — we have NO quality evidence, must never read as a
# pass) or ERROR (the model replied but its verdict was unparseable). DISABLED is
# the whole-tier state when Tier 2 is requested with no real grader configured.
BLOCKED, ERROR, DISABLED = "BLOCKED", "ERROR", "DISABLED"
# Worst-wins rank for the Tier-2 headline. BLOCKED/ERROR sit ABOVE FAIL on purpose:
# "I could not grade" must be the loudest state so an unreachable model is never
# mistaken for a clean result. The full status_counts are always printed, so a
# confirmed FAIL is never hidden underneath a BLOCKED headline.
_TIER2_RANK = {PASS: 0, WARN: 1, FAIL: 2, ERROR: 3, BLOCKED: 4}
TIER2_GRADERS = ("null", "fixture", "claude-cli")
# Direct Claude Code CLI (subscription auth), NOT the Anthropic API. Overridable by
# --tier2-cli-path or $HERMES_CLAUDE_CLI; the model by --tier2-model / $HERMES_TIER2_MODEL.
DEFAULT_CLAUDE_CLI = "/opt/homebrew/bin/claude"
DEFAULT_TIER2_MODEL = "claude-opus-4-8"
DEFAULT_TIER2_TIMEOUT = 120  # seconds per model call
# Env vars stripped from the CLI subprocess so it uses the Claude Code SUBSCRIPTION
# path and can neither bill nor leak a credential. Three vectors, all covered:
#   (1) direct credentials — would be leaked if inherited;
#   (2) host/endpoint overrides — would send the subscription OAuth bearer token to
#       a different (e.g. GBrain/attacker) host → credential exfiltration;
#   (3) paid-backend routing switches — would route inference to a billed cloud
#       backend (Bedrock/Vertex) instead of the subscription.
# We also pass --model explicitly, so an ambient model override is dropped too.
# (Denylist, not allowlist: an allowlist risks starving the CLI of vars it needs to
#  find its on-disk subscription creds — HOME/XDG/NODE_* — turning every real run
#  into a BLOCKED. This list is the complete set of known bill/leak vectors.)
_PROTECTED_API_ENV = (
    # (1) credentials
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
    "OPENAI_API_KEY",
    # (2) host / endpoint overrides (leak the subscription token to another host)
    "ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL", "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE",
    # (3) paid-backend routing switches
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
    # model overrides (we pass --model explicitly — avoid an ambient billed model)
    "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
)


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
            # bool() is load-bearing: `recall==1.0 and noise_h and ...` short-circuits to
            # the empty set when noise_h is empty, and a set is not JSON-serializable —
            # it would crash --json on any fully-recalled, noise-free fixture.
            gold_uncontested = bool(recall == 1.0 and noise_h and not noise_dropped)
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
# TIER 2 — model-backed answer-quality grading (gated, optional, OFF by default).#
#                                                                               #
# Tier 1 proves the needed entry was PRESENT. That is necessary, not sufficient: #
# presence is a proxy for answer quality, not answer quality itself. Tier 2      #
# closes the gap by asking a model the task TWICE — once with the FULL memory     #
# block, once with the PROJECTED block for the gating mode — and grading whether  #
# the projected answer still preserves the gold-required facts and honours the    #
# pinned constraints, relative to the full answer.                               #
#                                                                               #
# Safety rails baked in (see the tests):                                         #
#   * The default path NEVER instantiates a model grader and NEVER spawns a      #
#     subprocess: `--tier2` defaults to the NullGrader (DISABLED), and nothing   #
#     here is reached unless an operator explicitly asks for `claude-cli`.       #
#   * The real grader is the direct Claude Code CLI (subscription auth), invoked #
#     as a subprocess. API-key env vars are stripped (_PROTECTED_API_ENV) so it  #
#     can neither bill nor leak a GBrain/OpenAI API key.                         #
#   * A model that is unreachable / times out / exits nonzero is BLOCKED, not a  #
#     pass. An unparseable verdict is ERROR. Both are loud and exit nonzero.     #
#   * Status is DERIVED IN CODE from the model's structured findings (which gold #
#     facts it judged missing/violated), never assigned by the model's vibe —    #
#     the same "code decides status, not the grader" discipline as Tier 1.       #
#   * Preservation is CONSERVATIVE: a required fact counts as preserved only if   #
#     the model affirmatively says so; anything it leaves unconfirmed counts      #
#     against preservation, so the harness errs toward flagging quality loss.    #
# --------------------------------------------------------------------------- #
class GraderUnavailable(RuntimeError):
    """The grader/model could not be reached (missing CLI, timeout, nonzero exit).

    Maps to a BLOCKED task — we have NO quality evidence, which must never be read
    as a pass.
    """


class GraderError(RuntimeError):
    """The grader responded but its output could not be parsed → an ERROR task."""


class AnswerGrader:
    """Interface a Tier-2 grader implements. Bring your own model behind a flag.

    ``grade`` returns a raw verdict dict; ``run_tier2`` normalises it against the
    task's gold labels and DERIVES the PASS/WARN/FAIL/BLOCKED/ERROR status. A grader
    signals trouble with two reserved keys instead of raising across the boundary:

        {"unreachable": True, "error": "..."}   → BLOCKED   (no model evidence)
        {"parse_error":  True, "error": "..."}   → ERROR     (unparseable verdict)

    A successful verdict carries the model's structured findings:

        {"preserved_required": [labels], "missing_required": [labels],
         "violated_constraints": [pin-labels], "equivalence": "equivalent|degraded|broken",
         "rationale": "...", "full_answer": "...", "projected_answer": "..."}
    """

    name = "answer-grader"

    def grade(self, task: dict, full_block: str, projected_block: str) -> dict:
        raise NotImplementedError


class NullGrader(AnswerGrader):
    """Default: does nothing. Tier 2 is DISABLED unless a real grader is supplied.

    This is what guarantees the default path makes no model call: ``--tier2`` with no
    ``--tier2-grader`` lands here and the whole tier reports DISABLED (a loud no-op),
    never PASS.
    """

    name = "null"

    def grade(self, task: dict, full_block: str, projected_block: str) -> dict:
        return {"grader": "null", "disabled": True,
                "reason": "no model grader configured (Tier 1 only)"}


class FixtureGrader(AnswerGrader):
    """Replays hand-authored verdicts from a file — for tests + no-spend smoke runs.

    Proves the Tier-2 wiring, schema, and status policy end-to-end WITHOUT a model.
    Each verdict (keyed by task id) may simulate any outcome, including a blocked or
    unparseable grader, so the fail/blocked logic is exercisable deterministically.
    """

    name = "fixture"

    def __init__(self, verdicts: dict):
        self._verdicts = verdicts or {}

    def grade(self, task: dict, full_block: str, projected_block: str) -> dict:
        v = self._verdicts.get(task["id"])
        if v is None:
            return {"grader": "fixture", "unreachable": True,
                    "error": f"no fixture verdict supplied for task {task['id']!r}"}
        out = {"grader": "fixture", "model": v.get("model")}
        if v.get("unreachable"):
            out.update({"unreachable": True, "error": v.get("error", "simulated unreachable grader")})
            return out
        if v.get("parse_error"):
            out.update({"parse_error": True, "error": v.get("error", "simulated unparseable verdict")})
            return out
        out.update({
            "preserved_required": list(v.get("preserved_required") or []),
            "missing_required": list(v.get("missing_required") or []),
            "preserved_constraints": list(v.get("preserved_constraints")
                                          or v.get("honored_constraints") or []),
            "violated_constraints": list(v.get("violated_constraints") or []),
            "equivalence": v.get("equivalence", "equivalent"),
            "rationale": v.get("rationale", "fixture verdict"),
            "full_answer": v.get("full_answer"),
            "projected_answer": v.get("projected_answer"),
        })
        return out


def _sanitized_cli_env() -> dict:
    """os.environ minus every credential / host-override / paid-backend-routing var
    (_PROTECTED_API_ENV), so the CLI is pinned to subscription auth and can neither
    bill nor leak a GBrain/OpenAI key — even if those vars are set in the ambient env."""
    env = dict(os.environ)
    for k in _PROTECTED_API_ENV:
        env.pop(k, None)
    return env


def _default_cli_runner(cmd: list, prompt: str, timeout: int, env: dict) -> tuple:
    """The ONLY real subprocess boundary (injected with a fake in tests).

    Returns (returncode, stdout, stderr). Raises subprocess.TimeoutExpired /
    FileNotFoundError, which the grader translates into GraderUnavailable.
    """
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          timeout=timeout, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _resolve_cli(cli_path: str) -> str | None:
    """Resolve the CLI to an executable path, or None if it cannot be found.

    An absolute path must exist; a bare name is looked up on PATH. Done BEFORE any
    subprocess so a missing CLI is a clean BLOCKED, never a half-spawned call.
    """
    if os.path.isabs(cli_path):
        return cli_path if os.path.exists(cli_path) else None
    return shutil.which(cli_path)


class ClaudeCliGrader(AnswerGrader):
    """Real Tier-2 grader: the direct Claude Code CLI (subscription), as a subprocess.

    Three model calls per task: answer-under-FULL, answer-under-PROJECTED, then a
    judge call that compares the two against the gold facts/constraints. The judge
    returns structured JSON; ``run_tier2`` derives the status from it. No Anthropic/
    OpenAI API key is read or used (see _sanitized_cli_env).
    """

    name = "claude-cli"

    def __init__(self, *, cli_path: str = DEFAULT_CLAUDE_CLI, model: str = DEFAULT_TIER2_MODEL,
                 timeout: int = DEFAULT_TIER2_TIMEOUT, runner=None, answer_max_chars: int = 6000):
        self.cli_path = cli_path
        self.model = model
        self.timeout = timeout
        self._runner = runner or _default_cli_runner
        self.answer_max_chars = answer_max_chars

    def _ask(self, prompt: str) -> str:
        """One model turn. Raises GraderUnavailable on any reach/exec problem."""
        # Preflight only the real runner; an injected (test) runner needs no binary.
        resolved = self.cli_path
        if self._runner is _default_cli_runner:
            resolved = _resolve_cli(self.cli_path)
            if resolved is None:
                raise GraderUnavailable(
                    f"claude CLI not found at {self.cli_path!r}; install Claude Code, "
                    f"pass --tier2-cli-path, or set $HERMES_CLAUDE_CLI")
        cmd = [resolved, "-p", "--model", self.model]
        try:
            rc, out, err = self._runner(cmd, prompt, self.timeout, _sanitized_cli_env())
        except subprocess.TimeoutExpired as e:
            raise GraderUnavailable(f"claude CLI timed out after {self.timeout}s") from e
        except FileNotFoundError as e:
            raise GraderUnavailable(f"claude CLI not executable at {self.cli_path!r}: {e}") from e
        except OSError as e:
            raise GraderUnavailable(f"claude CLI could not be launched: {e}") from e
        if rc != 0:
            raise GraderUnavailable(
                f"claude CLI exited {rc}: {(err or '').strip()[:300] or '(no stderr)'}")
        text = (out or "").strip()
        if not text:
            raise GraderUnavailable("claude CLI returned empty output")
        return text

    def grade(self, task: dict, full_block: str, projected_block: str) -> dict:
        out = {"grader": self.name, "model": self.model}
        query = task.get("query") or f"(no query; respond for category {task.get('category','')!r})"
        try:
            ans_full = self._ask(_answer_prompt(query, full_block))
            ans_proj = self._ask(_answer_prompt(query, projected_block))
            required, pins = _tier2_required_and_pins(task)
            judge_raw = self._ask(_judge_prompt(query, ans_full, ans_proj, required, pins))
        except GraderUnavailable as e:
            out.update({"unreachable": True, "error": str(e)})
            return out
        parsed = _extract_json(judge_raw)
        if not isinstance(parsed, dict):
            out.update({"parse_error": True,
                        "error": "judge response was not a JSON object",
                        "raw": judge_raw[:500],
                        "full_answer": ans_full[:self.answer_max_chars],
                        "projected_answer": ans_proj[:self.answer_max_chars]})
            return out
        out.update({
            "preserved_required": parsed.get("preserved_required") or [],
            "missing_required": parsed.get("missing_required") or [],
            "preserved_constraints": parsed.get("preserved_constraints") or [],
            "violated_constraints": parsed.get("violated_constraints") or [],
            "equivalence": parsed.get("equivalence") or "degraded",
            "rationale": parsed.get("rationale") or "",
            "full_answer": ans_full[:self.answer_max_chars],
            "projected_answer": ans_proj[:self.answer_max_chars],
        })
        return out


# --- prompt construction ----------------------------------------------------- #
def _answer_prompt(query: str, block: str) -> str:
    """Build an answer prompt: respond to the user turn using ONLY the memory block."""
    return (
        "You are Hermes, a local engineering assistant. The following is the MEMORY "
        "available to you for this turn — treat it as everything you remember about the "
        "user and prior work. Do not invent facts beyond it.\n\n"
        "=== MEMORY (begin) ===\n"
        f"{block or '(empty)'}\n"
        "=== MEMORY (end) ===\n\n"
        f"User's request: {query}\n\n"
        "Answer the request concisely and concretely, grounded ONLY in the MEMORY above "
        "and the request. If the memory lacks something the answer needs, say exactly "
        "what is missing rather than guessing. Keep it under ~150 words."
    )


def _numbered(items: list) -> str:
    if not items:
        return "(none)"
    return "\n".join(f"{i+1}. [{it['label']}] {it['text']}" for i, it in enumerate(items))


def _judge_prompt(query: str, ans_full: str, ans_proj: str,
                  required: list, pins: list) -> str:
    """Build the judge prompt. The model returns ONLY structured JSON findings; the
    harness derives PASS/WARN/FAIL from them (the model never assigns the status)."""
    req_labels = [it["label"] for it in required]
    pin_labels = [it["label"] for it in pins]
    return (
        "You are grading whether a MEMORY-PROJECTION step preserved answer quality.\n\n"
        "Two answers were produced for the SAME user request, differing ONLY in the "
        "memory the assistant was given:\n"
        "  * FULL — produced with the entire memory.\n"
        "  * PROJECTED — produced with a compressed subset (the projection under test).\n\n"
        "A good projection yields a PROJECTED answer that still reflects every REQUIRED "
        "fact and honours every CONSTRAINT, relative to FULL.\n\n"
        f"User request:\n{query}\n\n"
        f"REQUIRED facts a correct answer must reflect (judge each by its label):\n"
        f"{_numbered(required)}\n\n"
        f"CONSTRAINTS that must never be dropped or violated (safety/identity/operational pins):\n"
        f"{_numbered(pins)}\n\n"
        f"FULL answer:\n{ans_full}\n\n"
        f"PROJECTED answer:\n{ans_proj}\n\n"
        "Judge the PROJECTED answer. Output ONLY a single JSON object, no prose, no code "
        "fence, with exactly these keys:\n"
        '{\n'
        '  "preserved_required": [REQUIRED labels the PROJECTED answer clearly reflects],\n'
        '  "missing_required": [REQUIRED labels PROJECTED drops, omits, or contradicts],\n'
        '  "preserved_constraints": [CONSTRAINT labels PROJECTED clearly still honours],\n'
        '  "violated_constraints": [CONSTRAINT labels PROJECTED drops or violates],\n'
        '  "equivalence": "equivalent" | "degraded" | "broken",\n'
        '  "rationale": "one sentence, <= 40 words"\n'
        '}\n'
        f"Use ONLY these REQUIRED labels: {req_labels}. "
        f"Use ONLY these CONSTRAINT labels: {pin_labels}. "
        "Attribute EXHAUSTIVELY: every REQUIRED label must appear in exactly one of "
        "preserved_required or missing_required, AND every CONSTRAINT label in exactly "
        "one of preserved_constraints or violated_constraints. Do not omit any label — a "
        "constraint you do not affirmatively place in preserved_constraints is treated as "
        "NOT honoured."
    )


def _extract_json(text: str):
    """Best-effort: parse a JSON object out of a model response (tolerates fences/prose).

    Strict parse first; else scan for balanced, string-aware ``{...}`` spans and return
    the LAST one that parses (the judge's final verdict). Balanced scanning (rather than
    first-brace..last-brace) avoids a stray brace in prose or a second JSON object
    swallowing the real object into an unparseable span — which would otherwise ERROR a
    perfectly valid verdict. Still fails closed (None → ERROR), never a wrong-but-valid parse.
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    candidates = []
    depth, start, in_str, esc = 0, None, False, False
    for idx, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start:idx + 1])
                start = None
    for cand in reversed(candidates):
        try:
            return json.loads(cand)
        except (ValueError, TypeError):
            continue
    return None


# --- Tier-2 orchestration ---------------------------------------------------- #
def _tier2_required_and_pins(task: dict) -> tuple:
    """Gold checklist for the judge: required facts and pinned constraints (label+text)."""
    required, pins = [], []
    for store_key in ("memory", "user"):
        for ent in (task.get(store_key) or []):
            label = ent.get("label") or MP.topic_of(ent["text"])[:48]
            if ent.get("required"):
                required.append({"label": label, "text": ent["text"]})
            if ent.get("pin") is not None:
                pins.append({"label": label, "text": ent["text"], "pin": ent["pin"]})
    return required, pins


def _task_blocks(task: dict, *, mode: str, today: _dt.date) -> tuple:
    """Render the FULL (unlimited-budget) and PROJECTED (gating-mode, task-budget)
    memory blocks for one task, in a throwaway temp home. Same projection path the
    engine ships, so the blocks are exactly what would be injected."""
    identity_extra = task.get("identity_extra")
    root = _materialize_home(task)
    try:
        full = _project(root, task, mode="static", budget=CONTROL_BUDGET,
                        today=today, identity_extra=identity_extra)
        proj = _project(root, task, mode=mode, budget=int(task["budget_tokens"]),
                        today=today, identity_extra=identity_extra)
        return full["projected_block"], proj["projected_block"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _preview(text, limit: int = 280):
    if not text:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _derive_tier2_status(*, required_total: int, missing: set, violated: list,
                         equivalence: str, floor: float, unconfirmed_pins=()) -> str:
    """Code decides status from the model's structured findings (never the model).

    A VIOLATED constraint or a 'broken' answer is a hard FAIL (safety-level). Required-
    fact preservation gates exactly like Tier-1 recall: below the floor → FAIL. A pin the
    grader did not affirmatively confirm honoured (``unconfirmed_pins``) means the safety
    property was not certified, so the task cannot PASS — it WARNs (never a silent PASS on
    constraint silence). Any required miss / unconfirmed pin / 'degraded' → WARN; all
    preserved + every pin confirmed + 'equivalent' → PASS.
    """
    if violated:
        return FAIL
    if equivalence == "broken":
        return FAIL
    if required_total:
        preserved_frac = (required_total - len(missing)) / required_total
        if preserved_frac < floor:
            return FAIL
    if missing or unconfirmed_pins or equivalence == "degraded":
        return WARN
    return PASS


def _normalize_verdict(task: dict, raw: dict, required: list, pins: list, *,
                       gating_mode: str, floor: float) -> dict:
    """Turn a grader's raw verdict into a canonical, status-bearing task verdict.

    Preservation is CONSERVATIVE: a required label counts as preserved ONLY if the
    model affirmatively listed it in preserved_required AND did not also flag it
    missing; anything unconfirmed counts as missing. The harness therefore errs
    toward reporting quality loss rather than hiding it.
    """
    req_labels = [r["label"] for r in required]
    pin_labels = [p["label"] for p in pins]
    pin_label_set = set(pin_labels)
    verdict = {
        "task_id": task["id"],
        "category": task.get("category", ""),
        "gating_mode": gating_mode,
        "grader": raw.get("grader", ""),
        "model": raw.get("model"),
        "required_total": len(req_labels),
        "pins_total": len(pin_labels),
        "preserved_required": [],
        "missing_required": [],
        "honored_constraints": [],
        "unconfirmed_constraints": [],
        "violated_constraints": [],
        "equivalence": None,
        "rationale": raw.get("rationale", ""),
        "full_answer_preview": _preview(raw.get("full_answer")),
        "projected_answer_preview": _preview(raw.get("projected_answer")),
        "error": raw.get("error"),
        "graded": False,
    }
    if raw.get("unreachable"):
        verdict["status"] = BLOCKED
        return verdict
    if raw.get("parse_error"):
        verdict["status"] = ERROR
        return verdict

    # Required facts: preserved ONLY if affirmed and not contradicted; else missing.
    explicit_missing = {l for l in (raw.get("missing_required") or []) if l in req_labels}
    explicit_preserved = {l for l in (raw.get("preserved_required") or []) if l in req_labels}
    preserved, missing = [], []
    for l in req_labels:
        if l in explicit_preserved and l not in explicit_missing:
            preserved.append(l)
        else:
            missing.append(l)  # explicitly missing, contradicted, OR unconfirmed

    # Constraints (pins): SAME conservatism. Honoured only if affirmed and not violated; a
    # pin the grader is silent on is UNCONFIRMED (safety property uncertified → cannot
    # PASS); an explicit violation is a hard FAIL. This closes the pin-only false-PASS hole.
    explicit_violated = {l for l in (raw.get("violated_constraints") or []) if l in pin_label_set}
    explicit_honored = {l for l in ((raw.get("preserved_constraints") or [])
                                    + (raw.get("honored_constraints") or [])) if l in pin_label_set}
    honored, unconfirmed, violated = [], [], []
    for l in pin_labels:
        if l in explicit_violated:
            violated.append(l)            # contradiction (honoured + violated) → violated
        elif l in explicit_honored:
            honored.append(l)
        else:
            unconfirmed.append(l)

    equivalence = raw.get("equivalence") or "degraded"
    if equivalence not in ("equivalent", "degraded", "broken"):
        equivalence = "degraded"

    verdict.update({
        "preserved_required": preserved,
        "missing_required": missing,
        "honored_constraints": sorted(honored),
        "unconfirmed_constraints": sorted(unconfirmed),
        "violated_constraints": sorted(violated),
        "equivalence": equivalence,
        "graded": True,
        "status": _derive_tier2_status(required_total=len(req_labels), missing=set(missing),
                                       violated=violated, unconfirmed_pins=unconfirmed,
                                       equivalence=equivalence, floor=floor),
    })
    return verdict


def run_tier2(tier1_report: dict, tasks: list, *, grader: AnswerGrader,
              today: _dt.date = DEFAULT_TODAY, gating_mode: str | None = None,
              recall_warn_floor: float = DEFAULT_RECALL_WARN_FLOOR,
              only_task: str | None = None, max_tasks: int | None = None) -> dict:
    """Run the model-backed answer-quality grade over the tasks. Pure orchestration:
    builds the two blocks per task, asks the grader, normalises + aggregates. The
    grader is the ONLY thing that may touch a model — and only ClaudeCliGrader does."""
    gating_mode = gating_mode or tier1_report.get("primary_mode") or "lexical"

    if isinstance(grader, NullGrader):
        return {
            "ran": True,
            "grader": "null",
            "model": None,
            "gating_mode": gating_mode,
            "overall_status": DISABLED,
            "note": "Tier 2 requested but no model grader configured — DISABLED (no "
                    "model call made). Pass --tier2-grader claude-cli (or fixture).",
            "status_counts": {PASS: 0, WARN: 0, FAIL: 0, BLOCKED: 0, ERROR: 0},
            "graded_count": 0,
            "blocked_count": 0,
            "tasks": [],
            "limitations": _TIER2_LIMITATIONS,
        }

    selected = tasks
    if only_task:
        selected = [t for t in tasks if t["id"] == only_task]
    if max_tasks is not None:
        selected = selected[:max_tasks]

    # An empty selection must NEVER read as a vacuous PASS (the silent-pass trap): a
    # mistyped --tier2-task or max_tasks=0 graded nothing, so there is no evidence.
    if not selected:
        why = (f"no task matched --tier2-task {only_task!r}" if only_task else
               "no tasks selected to grade" + (" (--tier2-max-tasks 0)" if max_tasks == 0 else ""))
        return {
            "ran": True, "grader": getattr(grader, "name", "answer-grader"), "model": None,
            "gating_mode": gating_mode, "recall_warn_floor": recall_warn_floor,
            "overall_status": BLOCKED, "note": why,
            "status_counts": {PASS: 0, WARN: 0, FAIL: 0, BLOCKED: 0, ERROR: 0},
            "graded_count": 0, "blocked_count": 0, "tasks_total": 0, "tasks": [],
            "limitations": _TIER2_LIMITATIONS,
        }

    verdicts = []
    for task in selected:
        required, pins = _tier2_required_and_pins(task)
        blocks_identical = False
        try:
            full_block, proj_block = _task_blocks(task, mode=gating_mode, today=today)
            blocks_identical = (full_block == proj_block)
            raw = grader.grade(task, full_block, proj_block)
        except GraderUnavailable as e:
            raw = {"grader": getattr(grader, "name", ""), "unreachable": True, "error": str(e)}
        except GraderError as e:
            raw = {"grader": getattr(grader, "name", ""), "parse_error": True, "error": str(e)}
        except Exception as e:  # noqa: BLE001 — isolate: one task's failure ≠ whole-run abort
            # An unexpected error in projection or a contract-violating grader becomes a
            # single ERROR verdict (loud, never a PASS), not an uncaught traceback that
            # drops grading for every remaining task.
            raw = {"grader": getattr(grader, "name", ""), "parse_error": True,
                   "error": f"internal grader/projection error: {type(e).__name__}: {e}"}
        verdict = _normalize_verdict(task, raw, required, pins,
                                     gating_mode=gating_mode, floor=recall_warn_floor)
        # Advisory only (never changes status): if projection kept everything, the FULL
        # and PROJECTED blocks are identical and the answers cannot differ — the grade is
        # uninformative (a trivially-true PASS), not evidence projection is safe.
        verdict["blocks_identical"] = blocks_identical
        if blocks_identical and verdict["status"] == PASS:
            verdict["advisory"] = ("projected block == full block at this budget; the "
                                   "comparison is vacuous — tighten budget to actually test projection")
        verdicts.append(verdict)

    status_counts = {PASS: 0, WARN: 0, FAIL: 0, BLOCKED: 0, ERROR: 0}
    for v in verdicts:
        status_counts[v["status"]] += 1
    graded_count = status_counts[PASS] + status_counts[WARN] + status_counts[FAIL]
    blocked_count = status_counts[BLOCKED] + status_counts[ERROR]
    overall = (max((v["status"] for v in verdicts), key=lambda s: _TIER2_RANK[s])
               if verdicts else PASS)

    model = next((v.get("model") for v in verdicts if v.get("model")), None)
    return {
        "ran": True,
        "grader": getattr(grader, "name", "answer-grader"),
        "model": model,
        "gating_mode": gating_mode,
        "recall_warn_floor": recall_warn_floor,
        "overall_status": overall,
        "status_counts": status_counts,
        "graded_count": graded_count,
        "blocked_count": blocked_count,
        "tasks_total": len(verdicts),
        "tasks": verdicts,
        "limitations": _TIER2_LIMITATIONS,
    }


_TIER2_LIMITATIONS = [
    "Tier 2 grades a MODEL's answers: it is non-deterministic and costs tokens; treat "
    "a single run as a sample, not a fixed measurement. Re-run or grade more tasks for "
    "confidence.",
    "The 'claude-cli' grader uses the direct Claude Code CLI (subscription auth); it "
    "makes ~3 model calls per task (answer-full, answer-projected, judge).",
    "A BLOCKED result means the grader was unreachable — NO quality evidence was "
    "obtained. It is never a pass; raise budget/availability and re-run.",
    "Preservation is judged conservatively (unconfirmed required facts count as "
    "missing), so Tier 2 errs toward flagging loss rather than hiding it.",
    "Token savings is still reported by Tier 1 only and NEVER decides a Tier-2 status.",
]


def load_tier2_fixture(path: str) -> dict:
    """Load a canned-verdicts file for the fixture grader: {"verdicts": {id: {...}}}."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as e:
        raise FixtureError(f"tier2 fixture not found: {path}") from e
    except json.JSONDecodeError as e:
        raise FixtureError(f"tier2 fixture is not valid JSON ({path}): {e}") from e
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(verdicts, dict):
        raise FixtureError(f"tier2 fixture must be an object with a 'verdicts' map: {path}")
    return verdicts


def build_grader(name: str, *, cli_path: str | None = None, model: str | None = None,
                 timeout: int = DEFAULT_TIER2_TIMEOUT, fixture_path: str | None = None) -> AnswerGrader:
    """Construct a grader by name. Only 'claude-cli' can ever reach a model; 'null'
    and 'fixture' never do. Resolution order for path/model: flag → env → default."""
    if name == "null":
        return NullGrader()
    if name == "fixture":
        if not fixture_path:
            raise FixtureError("--tier2-grader fixture requires --tier2-fixture PATH")
        return FixtureGrader(load_tier2_fixture(fixture_path))
    if name == "claude-cli":
        resolved_path = cli_path or os.environ.get("HERMES_CLAUDE_CLI") or DEFAULT_CLAUDE_CLI
        resolved_model = model or os.environ.get("HERMES_TIER2_MODEL") or DEFAULT_TIER2_MODEL
        return ClaudeCliGrader(cli_path=resolved_path, model=resolved_model, timeout=timeout)
    raise FixtureError(f"unknown tier2 grader {name!r}; choose one of {TIER2_GRADERS}")


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
    t2 = report.get("tier2")
    if t2 and t2.get("ran"):
        add(f"- **Tier 2 (answer quality):** {t2['overall_status']}  "
            f"(grader `{t2['grader']}`"
            + (f", model `{t2['model']}`" if t2.get("model") else "") + ")")
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

    t2 = report.get("tier2")
    if t2 and t2.get("ran"):
        add(render_tier2_markdown(t2))
        add("")

    add("## Limitations")
    add("")
    for lim in report["limitations"]:
        add(f"- {lim}")
    add("")
    return "\n".join(L)


def render_tier2_markdown(t2: dict) -> str:
    """Render the Tier-2 (answer-quality) section. Self-contained so it can be emitted
    standalone or appended to the Tier-1 markdown."""
    L = []
    add = L.append
    add("## Tier 2 — answer-quality preservation  (model-backed)")
    add("")
    if t2["overall_status"] == DISABLED:
        add(f"**DISABLED.** {t2.get('note', '')}")
        return "\n".join(L)
    # Empty selection (mistyped --tier2-task / --tier2-max-tasks 0): BLOCKED with a reason
    # and no rows. Print the reason instead of a self-contradictory "BLOCKED, 0 of 0 graded"
    # table, so a human can tell a config error from a real grader outage.
    if not t2.get("tasks") and t2.get("note"):
        add(f"**{t2['overall_status']}.** {t2['note']}")
        return "\n".join(L)

    sc = t2["status_counts"]
    add(f"- **Tier-2 verdict:** {t2['overall_status']}  "
        f"(PASS {sc[PASS]} · WARN {sc[WARN]} · FAIL {sc[FAIL]} · "
        f"BLOCKED {sc[BLOCKED]} · ERROR {sc[ERROR]} of {t2.get('tasks_total', 0)} graded)")
    add(f"- **Grader:** `{t2['grader']}`"
        + (f"  ·  **Model:** `{t2['model']}`" if t2.get("model") else "")
        + f"  ·  **Gating mode:** `{t2['gating_mode']}`")
    if t2.get("blocked_count"):
        add(f"- ⚠ **{t2['blocked_count']} task(s) could not be graded** (BLOCKED/ERROR). "
            "This is NOT a pass — the model produced no quality evidence for them.")
    add("")
    add("| Task | Status | Equivalence | Preserved | Missing required | Violated constraints |")
    add("|---|:--:|:--:|--:|---|---|")
    for v in t2["tasks"]:
        if v["status"] in (BLOCKED, ERROR):
            detail = (v.get("error") or "").strip()
            add(f"| {v['task_id']} | {v['status']} | — | — | "
                f"_{detail[:80] or 'no quality evidence'}_ | — |")
            continue
        preserved = f"{len(v['preserved_required'])}/{v['required_total']}"
        missing = ", ".join(v["missing_required"]) if v["missing_required"] else "—"
        violated = ", ".join(v["violated_constraints"]) if v["violated_constraints"] else "—"
        status_cell = v["status"] + (" ⚠" if v.get("advisory") else "")
        add(f"| {v['task_id']} | {status_cell} | {v.get('equivalence') or '—'} | "
            f"{preserved} | {missing} | {violated} |")
    add("")
    flagged = [v for v in t2["tasks"] if v["status"] != PASS]
    if flagged:
        add("### Tier-2 findings")
        for v in flagged:
            head = f"- **{v['task_id']} — {v['status']}**"
            if v["status"] in (BLOCKED, ERROR):
                add(f"{head}: {v.get('error') or 'grader produced no usable verdict'}")
            else:
                bits = []
                if v["violated_constraints"]:
                    bits.append(f"violated constraints: {v['violated_constraints']}")
                if v.get("unconfirmed_constraints"):
                    bits.append(f"unconfirmed pins (not certified honoured): "
                                f"{v['unconfirmed_constraints']}")
                if v["missing_required"]:
                    bits.append(f"missing required: {v['missing_required']}")
                if v.get("rationale"):
                    bits.append(f"_{v['rationale']}_")
                add(f"{head}: " + "; ".join(bits) if bits else head)
        add("")
    # Advisories (e.g. vacuous comparison) — surfaced for humans; never change status.
    advisories = [v for v in t2["tasks"] if v.get("advisory")]
    if advisories:
        add("### Tier-2 advisories  (⚠ — do not change status)")
        for v in advisories:
            add(f"- _{v['task_id']}: {v['advisory']}_")
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

    t2 = p.add_argument_group(
        "Tier 2 (optional, model-backed answer-quality grading)",
        "OFF by default. Tier 2 asks a model the task under FULL vs PROJECTED memory and "
        "grades whether the projected answer preserves the required facts/constraints. "
        "Only --tier2-grader claude-cli reaches a model (direct Claude Code CLI, "
        "subscription auth — no API key is read or used).")
    t2.add_argument("--tier2", action="store_true",
                    help="run Tier 2 after Tier 1 (default grader: null → DISABLED no-op)")
    t2.add_argument("--tier2-grader", choices=TIER2_GRADERS, default="null",
                    help="null (default, no model), fixture (replay verdicts, no model), "
                         "or claude-cli (real model)")
    t2.add_argument("--tier2-fixture", help="canned-verdicts JSON for --tier2-grader fixture")
    t2.add_argument("--tier2-model", default=None,
                    help=f"model id for claude-cli (default {DEFAULT_TIER2_MODEL}; "
                         f"env HERMES_TIER2_MODEL)")
    t2.add_argument("--tier2-cli-path", default=None,
                    help=f"path to the claude CLI (default {DEFAULT_CLAUDE_CLI}; "
                         f"env HERMES_CLAUDE_CLI)")
    t2.add_argument("--tier2-timeout", type=int, default=DEFAULT_TIER2_TIMEOUT,
                    help=f"per-call timeout seconds for claude-cli (default {DEFAULT_TIER2_TIMEOUT})")
    t2.add_argument("--tier2-task", help="grade only this task id (cheap smoke of the real grader)")
    t2.add_argument("--tier2-max-tasks", type=int, default=None,
                    help="grade at most this many tasks (cap token spend)")

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
    if args.tier2 and args.tier2_timeout <= 0:
        print("error: --tier2-timeout must be a positive integer (seconds)", file=sys.stderr)
        return 2
    modes = VALID_MODES if args.mode == "both" else (args.mode,)

    try:
        report = run_harness(args.tasks, modes=modes, today=today,
                             recall_warn_floor=args.recall_warn_floor)
    except FixtureError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # --- Tier 2 (opt-in). Building the grader is where a bad config (missing fixture,
    #     unknown grader) fails fast as a usage error; only 'claude-cli' reaches a model.
    if args.tier2:
        try:
            grader = build_grader(args.tier2_grader, cli_path=args.tier2_cli_path,
                                  model=args.tier2_model, timeout=args.tier2_timeout,
                                  fixture_path=args.tier2_fixture)
            tasks = load_tasks(args.tasks)["tasks"]
        except FixtureError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        today_used = _dt.date.fromisoformat(report["today"])
        report["tier2"] = run_tier2(
            report, tasks, grader=grader, today=today_used,
            gating_mode=report["primary_mode"], recall_warn_floor=args.recall_warn_floor,
            only_task=args.tier2_task, max_tasks=args.tier2_max_tasks)

    text = (json.dumps(report, indent=2, ensure_ascii=False) if args.json
            else render_markdown(report))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + ("\n" if not text.endswith("\n") else ""))
        status_note = report["overall_status"]
        if report.get("tier2", {}).get("ran"):
            status_note += f" · tier2 {report['tier2']['overall_status']}"
        print(f"wrote {args.out} ({status_note})", file=sys.stderr)
    else:
        print(text)

    # Combined exit code. FAIL (either tier) or strict-WARN → 1; an unreached/unparseable
    # Tier-2 grader (BLOCKED/ERROR) → 3 (loud, distinct from a quality FAIL); else 0.
    fail = bool(report["status_counts"][FAIL])
    warn = bool(report["status_counts"][WARN])
    t2 = report.get("tier2")
    t2_blocked = False
    if t2 and t2.get("ran"):
        sc = t2.get("status_counts", {})
        st = t2["overall_status"]
        # Decide from the COUNTS, not the single ranked headline. A confirmed Tier-2
        # FAIL must exit 1 even when a co-occurring BLOCKED makes BLOCKED the loudest
        # headline — otherwise a real safety/quality FAIL would be reported with the
        # 'grader unreachable' code 3 and a CI consumer could treat it as retryable.
        # `st in (BLOCKED, ERROR)` still catches the empty-selection case (counts all 0).
        fail = fail or sc.get(FAIL, 0) > 0
        warn = warn or sc.get(WARN, 0) > 0
        t2_blocked = (sc.get(BLOCKED, 0) + sc.get(ERROR, 0)) > 0 or st in (BLOCKED, ERROR)
    if fail:
        return 1
    if args.strict and warn:
        return 1
    if t2_blocked:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
