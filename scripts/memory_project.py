#!/usr/bin/env python3
"""Memory Projection Engine — Phase 1: Budget-Aware Static Projection.

Context management is a knapsack problem (Welihinda 2025; LongCodeZip; SWE-Pruner;
Entroly; TREACLE). Today Hermes injects ALL of MEMORY.md + USER.md into the system
prompt every turn (~4,800 tokens). Most turns need a fraction of that. This engine
selects the highest-VALUE memory entries that fit a TOKEN BUDGET, so the agent
spends its context on what matters instead of dumping everything.

    items      = memory entries (MEMORY.md / USER.md, §-delimited)
    value      = a multi-factor projection score (0..1)
    weight     = token cost of the entry (chars/4 estimate)
    capacity   = the token budget
    objective  = maximise total value subject to Σ weight ≤ budget   (0/1 knapsack)

Phase 1 is STATIC: it scores entries on intrinsic, query-independent signals
(importance, recency, specificity, hot-fit, always-inject). Phase 2 adds
query/context-awareness (re-rank against the live task); Phase 3 adds a feedback
loop (learn which projected entries actually got used). See skills/memory-projection.md.

DESIGN: this engine does NOT re-implement scoring. It REUSES memory_audit.py's
audited per-entry dimensions (durability, pointer_quality, specificity, hot_fit)
and the temporal layer's recorded_at for recency — the same signals the rest of
the stack already trusts. One source of truth, no drift (the INTEG-8 lesson).

Token estimate: ``tokens ≈ ceil(chars / 4)`` for English text — accurate to
~±10% for short memory entries, and avoids a tiktoken dependency (stdlib only).
Both the original and the projection are measured the same way, so the savings
percentage is a fair comparison even though the absolute count is approximate.

READ-ONLY: never modifies MEMORY.md / USER.md / the temporal DB. Output goes to
stdout (the projected block, or --json for the scoring report).

Run:
    python3 scripts/memory_project.py --home ~/.hermes --budget 2000
    python3 scripts/memory_project.py --home ~/.hermes --budget 2000 --json > report.json

stdlib only; reuses sibling memory-stack modules (degrades gracefully if temporal
is absent — recency then falls back to dates written in the entry text).
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

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# memory_audit is a HARD dependency: it owns the audited scoring dimensions we
# project on. A missing audit module is a broken install, not a soft-degrade.
import memory_audit as MA  # noqa: E402
import temporal_memory as TM  # noqa: E402
from memory_signals import ENTRY_DELIMITER, HEADER_SENTINEL, POINTER_SIGIL  # noqa: E402

TOOL_VERSION = "1.0.0"

# --------------------------------------------------------------------------- #
# Token model — the one estimator, used for both the original and the          #
# projection so the savings comparison is apples-to-apples.                    #
# --------------------------------------------------------------------------- #
CHARS_PER_TOKEN = 4
# Per-entry join overhead: entries are rendered "\n§\n"-joined. Charging one
# token of overhead per entry upper-bounds the rendered block's token count, so
# Σ(weights) ≤ budget GUARANTEES the rendered projection is ≤ budget (proof in
# tests). Without it, ceil() rounding of the joined block could nudge 1 over.
DELIM_TOKENS = 1


def est_tokens(text: str) -> int:
    """Estimate tokens for a string: ceil(chars / 4). The single source of truth
    for every token count in this module. Integer ceil, no float drift."""
    n = len(text)
    if n <= 0:
        return 0
    return (n + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def entry_weight(text: str) -> int:
    """Knapsack weight of one entry = its tokens + the §-join overhead."""
    return est_tokens(text) + DELIM_TOKENS


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- #
# Projection scoring weights (sum of the non-binary weights + always = 1.0).   #
# Documented in skills/memory-projection.md; tunable here in ONE place.        #
# --------------------------------------------------------------------------- #
W_IMPORTANCE = 0.25   # audit: (durability + pointer_quality) / 2
W_RECENCY = 0.15      # temporal: recorded_at, exp-decayed (fallback: date-in-text)
W_SPECIFICITY = 0.15  # audit: specificity_actionability
W_HOT_FIT = 0.10      # audit: hot_memory_fit (ideal length for a hot pointer)
W_ALWAYS = 0.15       # binary: matches an ALWAYS_INJECT pattern
W_RELEVANCE = 0.20    # Phase 2b: semantic closeness to the live task/query

DEFAULT_BUDGET = 2000
DEFAULT_RECENCY_HALFLIFE_DAYS = 30
DEFAULT_RELEVANCE_N = 20
DEFAULT_RELEVANCE_RESERVE_COUNT = 3
DEFAULT_RELEVANCE_RESERVE_THRESHOLD = 0.35
DEFAULT_STALE_DAYS = MA.DEFAULT_STALE_AFTER_DAYS
DEFAULT_MAX_ENTRY_CHARS = MA.DEFAULT_MAX_ENTRY_CHARS
# Recency when an entry carries no temporal record and no date in its text:
# neutral, not penalised — absence of a date is not evidence of staleness.
RECENCY_NEUTRAL = 0.5

# --------------------------------------------------------------------------- #
# ALWAYS_INJECT — the small, high-precision set the agent needs EVERY turn      #
# regardless of the current task. Matched on the entry TOPIC (its leading       #
# label / first line before the first colon), NOT anywhere in the body, so a    #
# preference that merely *mentions* "provider" is not mistaken for routing       #
# config. These entries get the binary 0.15 boost AND are force-included in the  #
# knapsack (mandatory items) — they are exempt from the budget by design.        #
#                                                                                #
# Override per-install with --always-inject-extra to force additional topics.    #
# Kept deliberately tight: a bloated mandatory set defeats the budget.           #
# --------------------------------------------------------------------------- #
OPERATIONAL_TOPIC_RE = re.compile(
    r"\b(routing|provider failover|failover automation|restart protocol|"
    r"model routing|provider config|fallback sequence|design resources|design-resource-index)\b", re.I)
SAFETY_PIN_RE = re.compile(
    r"\b(api key policy|execution safety|live execution|trade approval|"
    r"approval before.*(?:trade|live execution|live order)|"
    r"do not (?:type|share|expose|commit|log|print|hardcode|connect|place|expand|scale|automate|deploy|enable|gate|follow)|"
    r"don't (?:type|share|expose|commit|log|print|hardcode|connect|place|expand|scale|automate|deploy|enable|gate|follow)|"
    r"never (?:type|share|expose|commit|log|print|hardcode).*(?:credential|secret|password|api key|token)|"
    r"(?:no|never) (?:live|real)[- ]?(?:trade|trades|order|orders|execution|money)|"
    r"live (?:trade|trades|execution|order|orders)|"
    r"do not scale before proof|do not click.*(?:permission dialog|payment ui))\b", re.I | re.S)
IDENTITY_TOPIC_RE = re.compile(r"$^")  # opt-in/dynamic only; no shipped personal names
PIN_CLASSES = ("none", "safety", "identity", "operational")
_TOPIC_SCAN_CHARS = 70


def topic_of(text: str) -> str:
    """The entry's leading label — what the entry is ABOUT. The text up to the
    first colon if one appears early, else the first ~70 chars. Pointer sigil
    stripped so '↪ Hermes routing: …' reads as 'Hermes routing'."""
    t = text.strip()
    if t.startswith(POINTER_SIGIL):
        t = t[len(POINTER_SIGIL):].strip()
    head = t.split("\n", 1)[0]
    if ":" in head[:_TOPIC_SCAN_CHARS]:
        return head.split(":", 1)[0]
    return head[:_TOPIC_SCAN_CHARS]


def _has_topic_label(text: str) -> bool:
    head = text.strip().split("\n", 1)[0]
    return ":" in head[:_TOPIC_SCAN_CHARS]


def _identity_re_from_owner(user_home: str | None, identity_extra: str | None = None) -> re.Pattern | None:
    """Build an install-local identity matcher without shipping personal names.

    The default derives from the basename of --user-home (via memory_audit's
    owner_stopwords); users can add explicit topics with --identity-extra.
    """
    parts = []
    generic_owner_tokens = {"tmp", "temp", "test", "tests", "home", "user", "users", "project", "profile"}
    owner_tokens = [t for t in sorted(MA.owner_stopwords(user_home), key=len, reverse=True)
                    if t not in generic_owner_tokens]
    if owner_tokens:
        parts.append(r"\b(?:" + "|".join(re.escape(t) for t in owner_tokens) + r")\b")
    if identity_extra:
        parts.append(f"(?:{identity_extra})")
    return re.compile("|".join(parts), re.I) if parts else None


def _identity_topic_ok(topic: str, identity_re: re.Pattern | None) -> bool:
    if identity_re is None or not identity_re.search(topic):
        return False
    # A dynamic owner token should pin the compact identity heading (e.g.
    # "Jdoe:" / "Owner Name:"), not every project/status topic that
    # happens to mention the owner's name or home path.
    if "'s" in topic.lower():
        return False
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]*", topic)
    return 0 < len(words) <= 4


def pin_class_for(text: str, extra_re: re.Pattern | None = None,
                  identity_re: re.Pattern | None = None) -> str:
    """Classify non-negotiable entries that must bypass retrieval/budget gates.

    safety beats identity beats operational. Matching is deliberately conservative:
    safety can match the full entry body because missing a safety rule is worse
    than over-injecting one; identity/operational match the topic to prevent a
    body mention from pinning unrelated content.
    """
    stripped = text.strip()
    topic = topic_of(stripped)
    if SAFETY_PIN_RE.search(stripped):
        return "safety"
    if stripped.startswith(HEADER_SENTINEL):
        return "operational"
    if ((_has_topic_label(stripped) and _identity_topic_ok(topic, identity_re))
            or IDENTITY_TOPIC_RE.search(topic)):
        return "identity"
    if OPERATIONAL_TOPIC_RE.search(topic):
        return "operational"
    if extra_re is not None and extra_re.search(topic):
        return "operational"
    return "none"


def is_always_inject(text: str, extra_re: re.Pattern | None = None,
                     identity_re: re.Pattern | None = None) -> bool:
    """Backward-compatible boolean: any first-class pin is mandatory."""
    return pin_class_for(text, extra_re, identity_re) != "none"


# --------------------------------------------------------------------------- #
# Recency — pulled from the temporal layer's recorded_at / eff_valid_from,      #
# exponentially decayed. Degrades gracefully: no temporal layer → fall back to  #
# the most recent date written in the entry text → neutral default.            #
# --------------------------------------------------------------------------- #
_STORE_FILE = {"memory": "MEMORY.md", "user": "USER.md"}


def _parse_iso_date(value) -> _dt.date | None:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(s).date()
    except ValueError:
        try:
            return _dt.date.fromisoformat(s[:10])
        except ValueError:
            return None


def build_recency_index(home: str, db_path: str | None = None):
    """Map (temporal_store, content_hash) and (temporal_store, fact_key) → the
    most recent effective date of the current version of that fact.

    Returns (index_or_None, note). index is None when the temporal layer is
    unavailable (fresh install, import error) — callers then use the text-date
    fallback. Never raises: a broken temporal DB must not break projection.
    """
    try:
        import temporal_memory as TM  # noqa: WPS433
    except Exception as e:  # pragma: no cover - import-time only
        return None, f"temporal_memory unavailable ({e}); recency from in-text dates"
    try:
        tm = TM.TemporalMemory(home=home, db_path=db_path)
        try:
            rows = tm.current()
        finally:
            # close the sqlite handle now — projection only needs the rows + the
            # module-level hash/key helpers, not a live connection. An agent that
            # projects every turn must not leak a connection per call.
            tm.conn.close()
    except Exception as e:
        return None, f"temporal layer unreadable ({e}); recency from in-text dates"

    by_hash: dict[tuple, _dt.date] = {}
    by_key: dict[tuple, _dt.date] = {}
    for r in rows:
        d = _parse_iso_date(r.get("eff_valid_from") or r.get("recorded_at"))
        if d is None:
            continue
        store = r.get("store") or ""
        h, k = r.get("content_hash"), r.get("fact_key")
        if h:
            key = (store, h)
            if d > by_hash.get(key, _dt.date.min):
                by_hash[key] = d
        if k:
            key = (store, k)
            if d > by_key.get(key, _dt.date.min):
                by_key[key] = d
    return {"by_hash": by_hash, "by_key": by_key, "_tm": TM}, \
        f"temporal: {len(rows)} current facts indexed"


def recency_for(text: str, store: str, index, today: _dt.date,
                halflife_days: int) -> tuple[float, str]:
    """Recency score in [0,1] and its source. Exponential decay with the given
    half-life: 1.0 today, 0.5 at one half-life, 0.25 at two, never quite 0 (an
    old-but-undated durable fact keeps a little credit)."""
    d: _dt.date | None = None
    src = "none"
    tstore = _STORE_FILE.get(store, store)

    if index is not None:
        TM = index["_tm"]
        h = (tstore, TM.content_hash(text))
        if h in index["by_hash"]:
            d, src = index["by_hash"][h], "temporal:hash"
        else:
            k = (tstore, TM.derive_key(text))
            if k in index["by_key"]:
                d, src = index["by_key"][k], "temporal:key"

    if d is None:
        ld = MA.latest_date(text)
        if ld is not None:
            d, src = ld, "text-date"

    if d is None:
        return RECENCY_NEUTRAL, "neutral-default"

    age = max(0, (today - d).days)
    if halflife_days <= 0:
        return (1.0 if age == 0 else 0.0), src
    return clamp(math.pow(0.5, age / halflife_days)), src


# --------------------------------------------------------------------------- #
# Per-entry projection score — a weighted blend of audited dimensions.         #
# --------------------------------------------------------------------------- #
def score_components(audit_scores: dict, recency: float, always: bool,
                     relevance: float = 0.0) -> dict:
    """Map audit's dimensions onto projection factors. importance fuses
    durability (is this a standing fact vs a status blip?) with pointer_quality
    (does it point somewhere real?) — both are 'is this worth keeping' signals.

    Phase 2b adds relevance: semantic closeness to the live user turn/query. It is
    a soft boost, never a gate; unavailable/no-query relevance is 0.0 so the
    engine degrades to static Phase 1.
    """
    importance = clamp((audit_scores["durability"] + audit_scores["pointer_quality"]) / 2.0)
    return {
        "importance": round(importance, 4),
        "recency": round(clamp(recency), 4),
        "specificity": round(clamp(audit_scores["specificity_actionability"]), 4),
        "hot_fit": round(clamp(audit_scores["hot_memory_fit"]), 4),
        "always_inject": 1.0 if always else 0.0,
        "relevance": round(clamp(relevance), 4),
    }


def projection_score(components: dict) -> float:
    return clamp(
        W_IMPORTANCE * components["importance"]
        + W_RECENCY * components["recency"]
        + W_SPECIFICITY * components["specificity"]
        + W_HOT_FIT * components["hot_fit"]
        + W_ALWAYS * components["always_inject"]
        + W_RELEVANCE * components.get("relevance", 0.0))


def _local_chromadb_available() -> bool:
    try:
        import chromadb  # noqa: F401,WPS433
        import sentence_transformers  # noqa: F401,WPS433
        return True
    except Exception:
        return False


def _semantic_python_candidates() -> list[str]:
    explicit = os.environ.get("HERMES_MEMORY_ENTRY_PYTHON") or os.environ.get("HERMES_SEMANTIC_PYTHON")
    candidates = [explicit] if explicit else []
    candidates.extend(["python3.14", "/opt/homebrew/bin/python3.14", "/opt/homebrew/bin/python3"])
    out = []
    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        resolved = shutil.which(c) if not os.path.isabs(c) else c
        if resolved and os.path.exists(resolved) and os.path.realpath(resolved) != os.path.realpath(sys.executable):
            out.append(resolved)
    return out


def _subprocess_memory_hits(home: str, query: str, *, n_results: int) -> tuple[list, str]:
    """Search entry memory through a Python with Chroma deps.

    Hermes often runs the agent in Python 3.11 without ChromaDB while the semantic
    stack is installed under Python 3.14. Projection should still get semantic
    relevance, so fall back to the memory_entry_index CLI with stderr isolated.
    """
    if os.environ.get("HERMES_MEMORY_ENTRY_SUBPROCESS", "1") in {"0", "false", "False"}:
        return [], "subprocess-disabled"
    if os.environ.get("_HERMES_MEMORY_ENTRY_SUBPROCESS_CHILD"):
        return [], "subprocess-recursion-guard"
    script = os.path.join(_HERE, "memory_entry_index.py")
    if not os.path.exists(script):
        return [], "subprocess-missing-script"
    if not os.path.isdir(os.path.join(home, "chroma", "sessions")):
        return [], "no-memory-index-dir"
    last = "no-python3.14"
    for py in _semantic_python_candidates():
        env = os.environ.copy()
        env["HERMES_HOME"] = home
        env["_HERMES_MEMORY_ENTRY_SUBPROCESS_CHILD"] = "1"
        try:
            r = subprocess.run(
                [py, script, "search", query, "--home", home, "--n", str(max(1, int(n_results))), "--json"],
                capture_output=True, text=True, timeout=float(os.environ.get("HERMES_MEMORY_ENTRY_TIMEOUT", "45")), env=env,
            )
        except Exception as e:  # pragma: no cover - environment dependent
            last = f"{os.path.basename(py)}:{type(e).__name__}:{e}"
            continue
        if r.returncode == 0:
            try:
                payload = json.loads(r.stdout or "{}")
                return payload.get("results") or [], f"subprocess:{os.path.basename(py)}"
            except Exception as e:
                last = f"{os.path.basename(py)}:bad-json:{e}"
        else:
            last = f"{os.path.basename(py)}:exit{r.returncode}:{(r.stderr or r.stdout)[:160]}"
    return [], last


# --------------------------------------------------------------------------- #
# Phase 2b relevance — optional semantic closeness to the live task/query.      #
# --------------------------------------------------------------------------- #
def build_relevance_index(home: str, query: str | None, relevance_hits: list | None = None,
                          *, n_results: int = 20) -> tuple[dict, str]:
    """Return maps for semantic relevance keyed by content_hash and entry_ref.

    With no query this returns an empty map and an explicit disabled note. When
    the memory-entry semantic index is unavailable, it fails closed (empty map)
    so projection remains the proven static Phase-1 engine.

    ``relevance_hits`` is injectable for hermetic tests and has the same shape as
    memory_entry_index.search_memories().
    """
    if not query or not str(query).strip():
        return {"by_hash": {}, "by_ref": {}}, "disabled:no-query"
    try:
        hits = relevance_hits
        source = "injected"
        if hits is None:
            try:
                import memory_entry_index as MEI  # noqa: WPS433
                hits = MEI.search_memories(str(query), home, n_results=n_results)
                if hits:
                    source = (hits[0].get("__search_source") or "direct")
                else:
                    source = "daemon-or-direct-empty"
            except Exception as e:
                hits = []
                source = f"daemon-or-direct-error:{type(e).__name__}"
            if not hits:
                hits, sub_note = _subprocess_memory_hits(home, str(query), n_results=n_results)
                source = sub_note if hits else f"{source}+{sub_note}"
    except Exception as e:
        hits, sub_note = _subprocess_memory_hits(home, str(query), n_results=n_results)
        if not hits:
            return {"by_hash": {}, "by_ref": {}}, f"unavailable:{e};{sub_note}"
        source = sub_note

    by_hash, by_ref = {}, {}
    for h in hits or []:
        try:
            score = float(h.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        score = clamp(score)
        ch = h.get("content_hash")
        ref = h.get("entry_ref")
        if ch:
            by_hash[ch] = max(score, by_hash.get(ch, 0.0))
        if ref:
            by_ref[ref] = max(score, by_ref.get(ref, 0.0))
    return {"by_hash": by_hash, "by_ref": by_ref}, f"memories-index:{len(hits or [])} hits via {source}"


def relevance_for(text: str, ref: str, rel_index: dict) -> tuple[float, str]:
    """Look up semantic relevance for one entry by content_hash first, ref second."""
    if not rel_index:
        return 0.0, "none"
    try:
        import temporal_memory as TM  # noqa: WPS433
        ch = TM.content_hash(text)
        if ch in rel_index.get("by_hash", {}):
            return clamp(rel_index["by_hash"][ch]), "content_hash"
    except Exception:
        pass
    if ref in rel_index.get("by_ref", {}):
        return clamp(rel_index["by_ref"][ref]), "entry_ref"
    return 0.0, "none"


# --------------------------------------------------------------------------- #
# 0/1 knapsack — exact dynamic programming. Entry counts are small (≤ ~60 by    #
# the intake policy's 35-entry ceiling × 2 files), so the O(n·W) table is tiny. #
# --------------------------------------------------------------------------- #
_VALUE_SCALE = 100000  # score (0..1) → integer value, so DP compares cleanly


def knapsack(weights: list[int], values: list[float], capacity: int) -> list[int]:
    """Return the indices of the value-maximising subset with Σ weight ≤ capacity.

    Deterministic: items are considered in input order and reconstruction is a
    fixed back-walk, so identical input always yields identical output. Float
    values are scaled to ints to avoid accumulation drift in the comparisons.
    """
    n = len(weights)
    if n == 0 or capacity <= 0:
        return []
    total = sum(weights)
    # Fast path: everything fits — skip the table entirely (also bounds memory
    # when the budget is large, e.g. "budget larger than all entries").
    if total <= capacity:
        return list(range(n))
    cap = min(capacity, total)  # no point sizing the table past what can be used
    ivals = [max(0, round(v * _VALUE_SCALE)) for v in values]

    dp = [[0] * (cap + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        wi, vi = weights[i - 1], ivals[i - 1]
        prev, row = dp[i - 1], dp[i]
        for w in range(cap + 1):
            best = prev[w]
            if wi <= w:
                cand = prev[w - wi] + vi
                if cand > best:
                    best = cand
            row[w] = best

    chosen: list[int] = []
    w = cap
    for i in range(n, 0, -1):
        if dp[i][w] != dp[i - 1][w]:
            chosen.append(i - 1)
            w -= weights[i - 1]
    chosen.reverse()
    return chosen


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #
def _load_entries(memory_path: str, user_path: str, user_home: str, *,
                  today: _dt.date, stale_days: int, max_entry_chars: int) -> list[dict]:
    """Audit both hot-memory files and return their entries in file order
    (memory first, then user), each carrying its audited scores."""
    owner_stop = MA.owner_stopwords(user_home)
    entries: list[dict] = []
    for path, store in ((memory_path, "memory"), (user_path, "user")):
        af = MA.audit_file(path, store, user_home, max_entry_chars=max_entry_chars,
                           today=today, stale_days=stale_days, owner_stop=owner_stop)
        entries.extend(af["entries"])
    return entries


def project(home: str, *, budget: int = DEFAULT_BUDGET,
            user_home: str | None = None, today: _dt.date | None = None,
            recency_halflife_days: int = DEFAULT_RECENCY_HALFLIFE_DAYS,
            stale_days: int = DEFAULT_STALE_DAYS,
            max_entry_chars: int = DEFAULT_MAX_ENTRY_CHARS,
            memory_path: str | None = None, user_path: str | None = None,
            db_path: str | None = None,
            always_inject_extra: str | None = None,
            query: str | None = None,
            relevance_hits: list | None = None,
            relevance_n: int = DEFAULT_RELEVANCE_N,
            relevance_reserve_count: int = DEFAULT_RELEVANCE_RESERVE_COUNT,
            relevance_reserve_threshold: float = DEFAULT_RELEVANCE_RESERVE_THRESHOLD,
            identity_extra: str | None = None) -> dict:
    """Project the hot memory down to a token budget. Returns a report dict that
    includes the rendered projected block under ``projected_block``. Pure /
    read-only / deterministic given (files, temporal DB, today, params).
    """
    home = os.path.abspath(os.path.expanduser(home))  # so MP.project(home="~/.hermes") works
    today = today or _dt.date.today()
    user_home = os.path.abspath(os.path.expanduser(user_home)) if user_home \
        else os.path.expanduser("~")
    if memory_path is None or user_path is None:
        d_mem, d_usr = MA._default_paths(home)
        memory_path = memory_path or d_mem
        user_path = user_path or d_usr
    extra_re = re.compile(always_inject_extra, re.I) if always_inject_extra else None
    identity_re = _identity_re_from_owner(user_home, identity_extra)

    entries = _load_entries(memory_path, user_path, user_home, today=today,
                            stale_days=stale_days, max_entry_chars=max_entry_chars)
    recency_index, recency_note = build_recency_index(home, db_path)
    relevance_index, relevance_note = build_relevance_index(
        home, query, relevance_hits, n_results=relevance_n)

    # ---- score every entry -------------------------------------------------
    scored: list[dict] = []
    for e in entries:
        text = e["text"]
        pin_class = pin_class_for(text, extra_re, identity_re)
        always = pin_class != "none"
        rec, rec_src = recency_for(text, e["store"], recency_index, today,
                                   recency_halflife_days)
        rel, rel_src = relevance_for(text, e["ref"], relevance_index)
        comps = score_components(e["scores"], rec, always, rel)
        content_hash = TM.content_hash(text)
        fact_key = TM.derive_key(text)
        scored.append({
            "ref": e["ref"], "store": e["store"], "index": e["index"],
            "text": text, "chars": e["chars"], "tokens": entry_weight(text),
            "preview": e["preview"], "key": e["key"], "kind": e["kind"],
            "content_hash": content_hash, "fact_key": fact_key,
            "pin_class": pin_class,
            "always_inject": always, "recency_source": rec_src,
            "relevance_source": rel_src,
            "components": comps, "score": round(projection_score(comps), 4),
        })

    # ---- split mandatory (always-inject), turn-relevant reserve, optional ----
    mandatory = [s for s in scored if s["always_inject"]]
    optional = [s for s in scored if not s["always_inject"]]
    mandatory_tokens = sum(s["tokens"] for s in mandatory)
    remaining = budget - mandatory_tokens
    over_budget = remaining < 0

    # Phase 2b: reserve a small budgeted slice for top turn-relevant entries.
    # A pure score-knapsack can skip one highly relevant long entry in favour of
    # several medium static entries. That is optimal mathematically but bad for
    # context-aware projection. This reserve implements the architecture's
    # "turn-relevant top-k" lane while still respecting the token budget.
    relevance_reserved: set[int] = set()
    if query and remaining > 0 and relevance_reserve_count > 0:
        ranked = sorted(
            [(i, s) for i, s in enumerate(optional)
             if s["components"].get("relevance", 0.0) >= relevance_reserve_threshold],
            key=lambda x: (-x[1]["components"].get("relevance", 0.0), -x[1]["score"], x[1]["ref"]),
        )
        used = 0
        for i, s in ranked:
            if len(relevance_reserved) >= relevance_reserve_count:
                break
            if used + s["tokens"] <= max(0, remaining):
                relevance_reserved.add(i)
                used += s["tokens"]
        remaining -= used

    # ---- knapsack the remaining optional entries into the remaining capacity -
    pool = [s for i, s in enumerate(optional) if i not in relevance_reserved]
    chosen_pool = set(knapsack([s["tokens"] for s in pool],
                               [s["score"] for s in pool],
                               max(0, remaining)))
    chosen_refs = {pool[i]["ref"] for i in chosen_pool}
    for i, s in enumerate(optional):
        s["selected"] = (i in relevance_reserved) or (s["ref"] in chosen_refs)
        s["relevance_reserved"] = i in relevance_reserved
    for s in mandatory:
        s["selected"] = True
        s["relevance_reserved"] = False

    # ---- render (file order preserved) + measure ----------------------------
    selected = [s for s in scored if s["selected"]]
    projected_block = ENTRY_DELIMITER.join(s["text"] for s in selected)
    original_block = ENTRY_DELIMITER.join(s["text"] for s in scored)
    projected_tokens = est_tokens(projected_block)
    original_tokens = est_tokens(original_block)
    savings_pct = round((1 - projected_tokens / original_tokens) * 100, 1) \
        if original_tokens else 0.0

    mem_chars = _safe_size(memory_path)
    usr_chars = _safe_size(user_path)

    # recency-source breakdown (proof the temporal layer is actually feeding it)
    rec_breakdown: dict[str, int] = {}
    for s in scored:
        rec_breakdown[s["recency_source"]] = rec_breakdown.get(s["recency_source"], 0) + 1

    # relevance-source breakdown (proof the memories index is actually feeding it)
    rel_breakdown: dict[str, int] = {}
    for s in scored:
        rel_breakdown[s["relevance_source"]] = rel_breakdown.get(s["relevance_source"], 0) + 1

    # pin-class breakdown (mandatory injection surface by reason)
    pin_breakdown = {k: 0 for k in PIN_CLASSES}
    for s in scored:
        pin_breakdown[s.get("pin_class", "none")] = pin_breakdown.get(s.get("pin_class", "none"), 0) + 1

    per_entry = [{
        "entry_ref": s["ref"], "store": s["store"], "kind": s["kind"],
        "score": s["score"], "tokens": s["tokens"], "chars": s["chars"],
        "content_hash": s["content_hash"], "fact_key": s["fact_key"],
        "selected": s["selected"], "always_inject": s["always_inject"],
        "pin_class": s.get("pin_class", "none"),
        "relevance_reserved": s.get("relevance_reserved", False),
        "recency_source": s["recency_source"], "relevance_source": s["relevance_source"],
        "components": s["components"],
        "reason": _reason(s, over_budget), "preview": s["preview"],
    } for s in sorted(scored, key=lambda x: (-x["score"], x["ref"]))]

    return {
        "tool": "memory_project", "tool_version": TOOL_VERSION,
        "generated_at": today.isoformat(), "home": home,
        "params": {
            "budget_tokens": budget,
            "recency_halflife_days": recency_halflife_days,
            "stale_after_days": stale_days, "max_entry_chars": max_entry_chars,
            "chars_per_token": CHARS_PER_TOKEN, "user_home": user_home,
            "today": today.isoformat(), "query": query or "", "relevance_n": relevance_n,
            "identity_extra": identity_extra or "",
            "relevance_reserve_count": relevance_reserve_count,
            "relevance_reserve_threshold": relevance_reserve_threshold,
            "weights": {"importance": W_IMPORTANCE, "recency": W_RECENCY,
                        "specificity": W_SPECIFICITY, "hot_fit": W_HOT_FIT,
                        "always_inject": W_ALWAYS, "relevance": W_RELEVANCE},
        },
        # headline numbers (required schema)
        "budget_tokens": budget,
        "projected_tokens": projected_tokens,
        "original_tokens": original_tokens,
        "entries_total": len(scored),
        "entries_selected": len(selected),
        "entries_skipped": len(scored) - len(selected),
        "savings_pct": savings_pct,
        "always_inject_count": len(mandatory),
        "pinned_count": len(mandatory),
        "pin_breakdown": pin_breakdown,
        "relevance_reserved_count": sum(1 for s in scored if s.get("relevance_reserved")),
        "original_memory_chars": len(original_block),
        "projected_memory_chars": len(projected_block),
        # transparency extras. mandatory_tokens is the knapsack-weight basis
        # (it is what is subtracted from the budget), not the rendered basis.
        "mandatory_tokens": mandatory_tokens,
        "over_budget": over_budget,
        "original_memory_md_chars": mem_chars,
        "original_user_md_chars": usr_chars,
        "recency_source": recency_note,
        "recency_breakdown": rec_breakdown,
        "relevance_source": relevance_note,
        "relevance_breakdown": rel_breakdown,
        "per_entry": per_entry,
        "projected_block": projected_block,
    }


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _reason(s: dict, over_budget: bool) -> str:
    if s["always_inject"]:
        return f"{s.get('pin_class', 'operational')}-pin (mandatory, exempt from budget)"
    if s.get("relevance_reserved"):
        return f"selected as turn-relevant reserve (relevance={s['components'].get('relevance', 0):.3f}, {s['tokens']} tok)"
    if s["selected"]:
        return f"selected by knapsack (score={s['score']:.3f}, {s['tokens']} tok)"
    if over_budget:
        return f"skipped (budget consumed by mandatory; score={s['score']:.3f})"
    return f"skipped (did not fit budget; score={s['score']:.3f}, {s['tokens']} tok)"


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memory_project.py",
        description="Project Hermes hot memory (MEMORY.md / USER.md) down to a "
                    "token budget by knapsack over a multi-factor relevance score. "
                    "READ-ONLY: prints the projected block (or --json report) to stdout.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="EXAMPLES:\n"
               "  # projected block, ready to inject into the system prompt\n"
               "  memory_project.py --home ~/.hermes --budget 2000\n"
               "  # full scoring report (what was kept/skipped and why)\n"
               "  memory_project.py --home ~/.hermes --budget 2000 --json > report.json\n")
    p.add_argument("--home", help="Hermes home (default $HERMES_HOME or ~/.hermes)")
    p.add_argument("--user-home", help="OS home for resolving ~/ paths in entries "
                   "(default real $HOME; set to the profile root for self-contained profiles)")
    p.add_argument("--memory", help="explicit MEMORY.md path (default <home>/memories/MEMORY.md)")
    p.add_argument("--user", help="explicit USER.md path (default <home>/memories/USER.md)")
    p.add_argument("--db", help="temporal memory_versions.db path (default <home>/memory_versions.db)")
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                   help=f"token budget for the projection (default {DEFAULT_BUDGET})")
    p.add_argument("--recency-halflife-days", type=int, default=DEFAULT_RECENCY_HALFLIFE_DAYS,
                   help=f"recency exponential half-life in days (default {DEFAULT_RECENCY_HALFLIFE_DAYS})")
    p.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS,
                   help=f"staleness threshold passed to the audit (default {DEFAULT_STALE_DAYS})")
    p.add_argument("--max-entry-chars", type=int, default=DEFAULT_MAX_ENTRY_CHARS,
                   help=f"per-entry length band passed to the audit (default {DEFAULT_MAX_ENTRY_CHARS})")
    p.add_argument("--always-inject-extra", metavar="REGEX",
                   help="extra ALWAYS_INJECT topic pattern to force-include (matched on entry topic)")
    p.add_argument("--identity-extra", metavar="REGEX",
                   help="extra identity topic pattern to force-include without hardcoding names in the package")
    p.add_argument("--query", help="current user turn/task for Phase 2b context-aware projection")
    p.add_argument("--relevance-n", type=int, default=DEFAULT_RELEVANCE_N,
                   help=f"number of memory-index hits to use for relevance (default {DEFAULT_RELEVANCE_N})")
    p.add_argument("--relevance-reserve-count", type=int, default=DEFAULT_RELEVANCE_RESERVE_COUNT,
                   help=f"max turn-relevant entries to reserve before knapsack (default {DEFAULT_RELEVANCE_RESERVE_COUNT})")
    p.add_argument("--relevance-reserve-threshold", type=float, default=DEFAULT_RELEVANCE_RESERVE_THRESHOLD,
                   help=f"minimum relevance for reserve lane (default {DEFAULT_RELEVANCE_RESERVE_THRESHOLD})")
    p.add_argument("--today", help="override today's date (YYYY-MM-DD) for deterministic runs")
    p.add_argument("--json", action="store_true", help="emit the JSON scoring report instead of the block")
    p.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    home = os.path.abspath(os.path.expanduser(
        args.home or os.environ.get("HERMES_HOME") or "~/.hermes"))
    today = None
    if args.today:
        try:
            today = _dt.date.fromisoformat(args.today)
        except ValueError:
            print(f"error: --today must be YYYY-MM-DD, got {args.today!r}", file=sys.stderr)
            return 2
    if args.budget < 0:
        print("error: --budget must be ≥ 0", file=sys.stderr)
        return 2

    report = project(
        home, budget=args.budget, user_home=args.user_home, today=today,
        recency_halflife_days=args.recency_halflife_days, stale_days=args.stale_days,
        max_entry_chars=args.max_entry_chars, memory_path=args.memory,
        user_path=args.user, db_path=args.db,
        always_inject_extra=args.always_inject_extra,
        identity_extra=args.identity_extra,
        query=args.query, relevance_n=args.relevance_n,
        relevance_reserve_count=args.relevance_reserve_count,
        relevance_reserve_threshold=args.relevance_reserve_threshold)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(report["projected_block"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
