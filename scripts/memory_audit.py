#!/usr/bin/env python3
"""Hermes MEMORY.md / USER.md audit — Area 2 of the onboarding remediation pipeline.

A **read-only** quality assessment of hot memory. It parses the §-delimited hot
files, classifies and scores each entry, cross-references file paths, detects
near-duplicates and obvious contradictions deterministically (no LLM, no network,
no external model), and recommends a per-entry action. It NEVER rewrites
MEMORY.md / USER.md — Area 3 (pointer rewrite) does that, gated on this report.

It is the read-time / batch counterpart to the write-time intake gate
(``hermes_memory_intake_gate.py``): the gate stops junk at write; this audit
finds junk that already landed and tells you what to do about it.

Design references:
  * ~/.hermes/plans/memory-onboarding-remediation.md  (Area 2)
  * notes/hermes/memory-intake-policy.md               (MEMORY.md = hot pointers only)
  * scripts/temporal_memory.py / hermes_memory_intake_gate.py (shared format + signals)

SAFETY:
  * Pure read-only. The only write is an explicit ``--out`` report path, and the
    tool refuses if ``--out`` resolves to an input hot file (no clobber).
  * Deterministic: same input -> same report. stdlib only.
  * Conservative by design: it will not recommend dropping a durable preference,
    and broken-pointer / duplicate / contradiction detection are tuned to avoid
    false positives on useful memory.

Usage:
    python3 memory_audit.py                      # audit default hot files -> markdown
    python3 memory_audit.py --json               # machine-readable report
    python3 memory_audit.py --out /tmp/report.md # write report (never an input file)
    python3 memory_audit.py --memory FILE --user FILE --home DIR --user-home DIR
    python3 memory_audit.py --max-entry-chars 280 --strict
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from memory_signals import (  # noqa: E402 — shared signal source of truth (INTEG-8)
    ENTRY_DELIMITER, POINTER_SIGIL, HEADER_SENTINEL,
    TEMPORAL_RE, COMPLETION_RE, METRIC_RE, LEADING_DATE_RE,
    PREF_RE, POINTER_RE, REFLECTION_RE,
)

TOOL_VERSION = "1.1.0"

# Format constants (ENTRY_DELIMITER / POINTER_SIGIL / HEADER_SENTINEL) and the
# shared durability regexes are imported from memory_signals (INTEG-8) — the one
# place they are defined, so the audit and the intake gate never drift apart.

# Budget defaults (mirror config/memory-defaults.yaml + the intake policy).
DEFAULT_MEMORY_CHAR_LIMIT = 15000
DEFAULT_USER_CHAR_LIMIT = 6000
DEFAULT_ENTRY_TARGET = 25
DEFAULT_ENTRY_CEILING = 35
DEFAULT_STALE_AFTER_DAYS = 30
# Capacity flags. AUTHORITATIVE values live in memory_health.py (WARN_PCT=80,
# CRIT_PCT=90); these mirror them so the audit and the health check agree, and
# config/memory-defaults.yaml documents the same numbers. Keep all three in sync.
CAPACITY_WARN_PCT = 80
CAPACITY_CRIT_PCT = 90

# Per-entry length bands (a hot pointer is ~one line).
DEFAULT_MAX_ENTRY_CHARS = 350
POINTER_IDEAL_CHARS = 200
DUMP_CHARS = 420
USER_PREF_IDEAL_CHARS = 420
USER_PREF_MAX_CHARS = 700

# Duplicate thresholds (token Jaccard over normalized bag-of-words).
DUP_NEAR = 0.45
DUP_STRONG = 0.60
STRICT_DUP_NEAR = 0.35
STRICT_MAX_ENTRY_CHARS = 240
# Contradiction gates.
CONTRA_SUBJECT_MIN = 0.18      # minimum subject overlap to consider any conflict
CONTRA_POLARITY_MIN = 0.45     # enabled-vs-disabled needs strong same-subject overlap

# Approximate chars left after acting on an entry (for shrink estimates only).
_ACTION_RESIDUAL = {
    "rewrite_to_pointer": 120, "merge": 120, "archive_to_note": 120,
    "remove_after_archive": 0, "move_to_skill": 80, "move_to_note": 80,
    "verify_current": None, "user_review": None, "keep": None,
}

# --------------------------------------------------------------------------- #
# Linguistic signals. The SHARED durable-vs-transient set — TEMPORAL_RE,       #
# COMPLETION_RE, METRIC_RE, LEADING_DATE_RE, PREF_RE, POINTER_RE, REFLECTION_RE #
# — is imported from memory_signals (INTEG-8). The regexes below are audit-only #
# refinements layered on that shared set; they are not used by the intake gate. #
# --------------------------------------------------------------------------- #
# Ephemeral EVENT verbs — describe a finished task (status). Excludes the
# durable-fact verbs confirmed/verified (which often describe enduring truths).
EVENT_VERB_RE = re.compile(
    r"\b(fixed|done|resolved|shipped|merged|upgraded to|repaired|wired|"
    r"completed?|installed|deployed|finished|found that|turns out|"
    r"discovered|switched to|migrated|rolled back|passes|broke|crashed|"
    r"applied|replaced)\b", re.I)
DURABLE_VERB_RE = re.compile(r"\b(confirmed|verified)\b", re.I)
# Non-whitelisted activity verbs that still indicate a dated activity-log line.
ACTIVITY_RE = re.compile(
    r"\b(reorganized|renamed|rewrote|rewritten|refactored|tuned|tweaked|"
    r"adjusted|bumped|cleaned\s*up|cleaned|explored|reworked|reordered|"
    r"relocated|trimmed|pruned|tightened|loosened)\b", re.I)
# Inline date anywhere in the line (date normalisation for dup detection + staleness).
INLINE_DATE_RE = re.compile(r"\(?\b20\d{2}-\d{2}-\d{2}\b\)?")
TODO_RE = re.compile(r"\b(todo|tbd|wip|fixme|pending|next step|to do|follow[- ]?up)\b", re.I)
DEBUG_RE = re.compile(
    r"\b(bug|error|traceback|exception|stack trace|root cause|because of|"
    r"crashed because|the .* broke|failed because|stale module|importerror|"
    r"sigkill|race condition|deadlock)\b", re.I)
PROGRESS_RE = re.compile(r"(phase\s*\d|phase\s+[ivx]+\b|milestone|in progress|✅)", re.I)
VAGUE_RE = re.compile(
    r"\b(stuff|things?|various|etc|some\b|misc|whatever|needs? work|tbd|better|improve)\b", re.I)
STRONG_VAGUE_RE = re.compile(
    r"\b(needs? work|needs? improvement|stuff|things?|whatever|tbd|various|misc)\b", re.I)
VOLATILE_RE = re.compile(
    r"\b(is (the )?(now )?default|default is|active_build|active build|"
    r"is enabled|is disabled|is paused|is active|now using|currently using|"
    r"is primary|set to)\b", re.I)
POLARITY_POS = re.compile(r"\b(enabled|active|on\b|running|connected|live|works|working)\b", re.I)
POLARITY_NEG = re.compile(r"\b(paused|disabled|off\b|removed|deprecated|stopped|killed|down|broken|not working)\b", re.I)

# Declared-default extraction (for contradiction detection).
DEFAULT_DECL_RE = re.compile(
    r"\b([\w.\-]{2,})\s+is\s+(?:(?:the|now|currently|a|new|primary|still)\s+){0,4}default\b", re.I)
DEFAULT_DECL_RE2 = re.compile(
    r"\bdefault\b[^.]{0,30}?\bis\s+(?:(?:now|currently|already|still)\s+){0,2}([\w.\-]*[\w\-])", re.I)
DEFAULT_TARGET_RE = re.compile(
    r"\b(?:switch|switched|switching|move|moved|moving|change|changed|changing|"
    r"set|setting|using|use)\b[^.]{0,40}?\bto\s+([\w.\-]*[\w\-])", re.I)
DEFAULT_AS_RE = re.compile(r"\b([\w.\-]*[\w\-])\s+as\s+(?:the\s+)?default\b", re.I)
# Salient version/subsystem identifiers that _subject_tokens drops; differing
# identifiers mean two entries are about DIFFERENT things (complementary, not
# contradictory).
IDENTIFIER_RE = re.compile(r"\bv\d+(?:\.\d+)*\b|\b\d+(?:\.\d+)+\b|\b[a-z]+\d+\b|\b\d+[a-z]+\b", re.I)

# Filesystem path candidates (rooted at ~/ or absolute). Only FILE paths (known
# extension) are existence-checked, so dirs / space-truncated tokens / model
# names like "/grok-build-0.1" never produce a false "broken pointer".
PATH_RE = re.compile(
    r"`?(~/[^\s`'\")]+|/Users/[^\s`'\")]+|/[A-Za-z0-9._\-]+(?:/[^\s`'\")]+)+)`?")
KNOWN_FILE_EXTS = {
    "md", "py", "json", "yaml", "yml", "sh", "txt", "js", "ts", "tsx", "jsx",
    "csv", "db", "sqlite", "png", "jpg", "jpeg", "pdf", "toml", "ini", "cfg",
    "log", "html", "css", "ipynb", "zip",
}

# Generic English / structural stopwords. NOTE: the owner's name is NOT hardcoded
# here — it is derived per-run from --user-home / --owner-name so duplicate and
# contradiction detection behave identically for every user. "hermes" stays
# (product name present in every install).
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "via", "are",
    "was", "has", "have", "not", "but", "all", "any", "use", "used", "user",
    "hermes", "when", "then", "must", "should", "after", "before",
    "current", "currently", "default", "also", "per", "set", "now", "new",
    "first", "still", "each", "every", "over", "without", "their", "they",
    "them", "than", "more", "less", "one", "two", "three", "can", "will",
    "his", "her", "its", "out", "you", "want", "wants", "uses",
}
DUP_BOILERPLATE = {"archived", "find", "session_search", "spine", "search",
                   "memories", "archive", "curator", "notes"}

KINDS = ("header", "pointer", "preference_fact", "content_dump", "status_update",
         "debugging_finding", "project_progress", "todo_temporary", "malformed")
ACTIONS = ("keep", "rewrite_to_pointer", "archive_to_note", "merge", "verify_current",
           "move_to_skill", "move_to_note", "remove_after_archive", "user_review")


# --------------------------------------------------------------------------- #
# Pure helpers                                                                #
# --------------------------------------------------------------------------- #
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path: str) -> str:
    # errors="replace" so a stray non-UTF8 byte degrades to U+FFFD instead of
    # crashing the whole read-only audit (lossless for valid UTF-8).
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def first_line(text: str) -> str:
    t = text.strip()
    return t.splitlines()[0].strip() if t else ""


def is_pointer(text: str) -> bool:
    return text.strip().startswith(POINTER_SIGIL)


def is_header(text: str) -> bool:
    return text.strip().startswith(HEADER_SENTINEL)


def norm_tokens(text: str, owner_stop: frozenset = frozenset()) -> set[str]:
    """Bag-of-words for similarity. Dates/paths normalized so paraphrases that
    differ only in date collapse; archived-pointer boilerplate + owner name
    dropped so distinct pointers / a name prefix don't inflate similarity."""
    t = INLINE_DATE_RE.sub(" DATE ", text)
    toks = set(re.findall(r"[a-z][a-z0-9]{3,}", t.lower())) - STOPWORDS - DUP_BOILERPLATE - owner_stop
    for p in PATH_RE.findall(text):
        norm = re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", p.lower())
        if "_archive/curator" in norm:
            continue
        toks.add(norm)
    return toks


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cosine_bow(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / ((len(a) ** 0.5) * (len(b) ** 0.5))


def extract_dates(text: str) -> list[_dt.date]:
    out = []
    for m in re.findall(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text):
        try:
            out.append(_dt.date(int(m[0]), int(m[1]), int(m[2])))
        except ValueError:
            continue
    return out


def latest_date(text: str) -> _dt.date | None:
    dates = extract_dates(text)
    return max(dates) if dates else None


def extract_paths(text: str) -> list[str]:
    out = []
    for m in PATH_RE.findall(text):
        p = m.strip().rstrip(").,;:`'\"")
        if p and (p.startswith("~/") or p.startswith("/")):
            out.append(p)
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def is_file_path(p: str) -> bool:
    base = p.rstrip("/")
    last = base.rsplit("/", 1)[-1]
    if "." not in last:
        return False
    return last.rsplit(".", 1)[-1].lower() in KNOWN_FILE_EXTS


def resolve_path(p: str, user_home: str) -> str:
    if p.startswith("~/"):
        return os.path.join(user_home, p[2:])
    if p == "~":
        return user_home
    return p


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def owner_stopwords(user_home: str | None, owner_name: str | None = None) -> frozenset:
    """Per-run name tokens to ignore for similarity — derived from --owner-name
    or the basename of --user-home (so 'jdoe' is stripped for /Users/jdoe just
    as 'asmith' is for the /Users/asmith home). No hardcoded username."""
    out: set[str] = set()
    for src in (owner_name, os.path.basename(os.path.normpath(user_home)) if user_home else None):
        if src:
            out |= {t for t in re.findall(r"[a-z][a-z0-9]{2,}", src.lower())}
    return frozenset(out)


# --------------------------------------------------------------------------- #
# Parsing                                                                     #
# --------------------------------------------------------------------------- #
def parse_entries(path: str) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    raw = read_text(path)
    entries = []
    for i, chunk in enumerate(raw.split(ENTRY_DELIMITER)):
        text = chunk.strip()
        if not text:
            continue
        entries.append({"index": len(entries), "raw_index": i, "text": text})
    return entries


# --------------------------------------------------------------------------- #
# Per-entry classification                                                    #
# --------------------------------------------------------------------------- #
def signals_for(text: str) -> dict:
    low = text.lower()
    return {
        "temporal": bool(TEMPORAL_RE.search(low)),
        "completion": bool(COMPLETION_RE.search(low)),
        "activity": bool(COMPLETION_RE.search(low) or ACTIVITY_RE.search(low)),
        "metric": bool(METRIC_RE.search(text)),
        "leading_date": bool(LEADING_DATE_RE.search(text)),
        "inline_date": bool(INLINE_DATE_RE.search(text)),
        "pointer": bool(POINTER_RE.search(text)),
        "preference": bool(PREF_RE.search(low)),
        "reflection": bool(REFLECTION_RE.search(text)),
        "todo": bool(TODO_RE.search(low)),
        "debug": bool(DEBUG_RE.search(low)),
        "progress": bool(PROGRESS_RE.search(text)),
        "vague": bool(VAGUE_RE.search(low)),
        "volatile": bool(VOLATILE_RE.search(low)),
        "pointer_sigil": is_pointer(text),
    }


def classify_kind(text: str, store: str, sig: dict, n: int, max_entry_chars: int) -> str:
    """First decisive signal wins. A durable PREFERENCE is never demoted to a
    status_update by an incidental metric/date — that protects useful hot memory
    from being recommended for deletion (the tool's #1 false-positive risk)."""
    if is_header(text):
        return "header"
    if sig["pointer_sigil"]:
        return "pointer"
    if sig["reflection"]:
        return "status_update"
    if sig["debug"] and not sig["preference"]:
        return "debugging_finding"

    # Dated status / activity log: a date/temporal anchor + a real EVENT/activity
    # verb or a (non-preference) metric. confirmed/verified alone (durable-fact
    # verbs) count only with a temporal marker. Never fires on a preference.
    durable_verb_only = bool(DURABLE_VERB_RE.search(text)) and not bool(EVENT_VERB_RE.search(text))
    event_signal = (sig["completion"] or sig["activity"]) and not (durable_verb_only and not sig["temporal"])
    metric_status = sig["metric"] and not sig["preference"]
    if not sig["preference"] and (sig["leading_date"] or sig["temporal"] or sig["inline_date"]) \
            and (event_signal or metric_status):
        return "project_progress" if sig["progress"] else "status_update"
    # Metric-only snapshot (no date), non-preference, non-pointer.
    if metric_status and not sig["pointer"]:
        return "status_update"
    # TODO / temporary.
    if sig["todo"] and not sig["pointer"]:
        return "todo_temporary"
    # Project progress (phases/milestones).
    if sig["progress"] and (sig["inline_date"] or sig["metric"]):
        return "project_progress"
    if sig["progress"] and sig["completion"]:  # "Phase N complete" w/o date/metric
        return "project_progress"
    # Malformed / too vague (checked before preference so "X needs work" isn't a pref).
    if STRONG_VAGUE_RE.search(text) and n < 80 and not sig["pointer"]:
        return "malformed"
    # Durable preference / fact.
    if sig["preference"] and not (sig["leading_date"] or sig["completion"]):
        return "preference_fact"
    # Content dump: long, non-pointer prose. For MEMORY.md the floor is the
    # user's own per-entry limit (closes the 351-419 keep-zone for path-less prose).
    dump_floor = max_entry_chars if store == "memory" else USER_PREF_MAX_CHARS
    if n > dump_floor and not sig["pointer"]:
        return "content_dump"
    if sig["pointer"]:
        if store == "memory" and n > DUMP_CHARS:
            return "content_dump"
        return "pointer"
    if sig["vague"] or n < 25:
        return "malformed"
    return "preference_fact"


# --------------------------------------------------------------------------- #
# Scoring                                                                     #
# --------------------------------------------------------------------------- #
def score_entry(text: str, store: str, sig: dict, n: int, kind: str,
                paths_missing: bool, today: _dt.date, stale_days: int) -> dict:
    d = 0.5
    if sig["preference"] and not (sig["temporal"] or sig["leading_date"]):
        d += 0.3
    if sig["pointer"]:
        d += 0.15
    if sig["completion"] and (sig["temporal"] or sig["leading_date"]):
        d -= 0.4
    if sig["metric"] and not sig["preference"]:
        d -= 0.25
    if sig["leading_date"]:
        d -= 0.2
    if kind in ("status_update", "debugging_finding", "project_progress", "todo_temporary"):
        d -= 0.2
    if kind == "header":
        d = 1.0
    durability = clamp(d)

    ideal = POINTER_IDEAL_CHARS if store == "memory" else USER_PREF_IDEAL_CHARS
    fit = 1.0 - clamp((n - ideal) / max(ideal, 1) * 0.5, 0.0, 0.7)
    if kind == "content_dump":
        fit -= 0.3
    if kind in ("status_update", "debugging_finding"):
        fit -= 0.2
    if kind in ("header", "pointer"):
        fit = max(fit, 0.85)
    hot_fit = clamp(fit)

    if kind == "header":
        pq = 1.0
    elif sig["pointer"]:
        pq = 1.0 if (n <= (DUMP_CHARS if store == "memory" else USER_PREF_MAX_CHARS)) else 0.6
        if paths_missing:
            pq = min(pq, 0.4)
    elif kind == "preference_fact":
        pq = 0.8
    else:
        pq = 0.2
    pointer_quality = clamp(pq)

    has_concrete = bool(extract_paths(text)) or bool(re.search(r"\b[A-Z][\w.\-]{2,}\b", text)) \
        or bool(re.search(r"\d", text))
    spec = 0.7 if has_concrete else 0.4
    if sig["vague"]:
        spec -= 0.3
    if n < 25:
        spec -= 0.2
    specificity = clamp(spec)

    s = 0.0
    if sig["temporal"]:
        s += 0.3
    if sig["completion"] and (sig["temporal"] or sig["leading_date"] or sig["inline_date"]):
        s += 0.3
    if sig["metric"]:
        s += 0.15
    ld = latest_date(text)
    if ld is not None:
        age = (today - ld).days
        if age > stale_days:
            s += clamp(0.2 + (age - stale_days) / 180.0 * 0.4, 0.0, 0.6)
        elif sig["leading_date"] or sig["inline_date"]:
            s += 0.1
    if sig["volatile"]:
        s += 0.2
    # Preference rebate, but NOT for entries carrying a concrete date (keep their
    # staleness signal even if they slip through as preference_fact).
    if kind == "preference_fact" and not sig["temporal"] \
            and not (sig["leading_date"] or sig["inline_date"]):
        s -= 0.2
    if kind == "header":
        s = 0.0
    staleness = clamp(s)

    overall = clamp(
        0.30 * durability + 0.30 * hot_fit + 0.20 * pointer_quality +
        0.20 * specificity - 0.25 * staleness)
    return {
        "durability": round(durability, 2),
        "hot_memory_fit": round(hot_fit, 2),
        "pointer_quality": round(pointer_quality, 2),
        "specificity_actionability": round(specificity, 2),
        "staleness_risk": round(staleness, 2),
        "overall_quality": round(overall, 2),
    }


# --------------------------------------------------------------------------- #
# Recommended action (deterministic precedence)                              #
# --------------------------------------------------------------------------- #
def recommend(entry: dict, store: str, max_entry_chars: int) -> tuple[str, str]:
    sig = entry["signals"]
    kind = entry["kind"]
    n = entry["chars"]
    flags = entry["flags"]
    has_target = bool(flags.get("has_real_target")) or sig["pointer_sigil"]

    if flags.get("possible_contradiction"):
        return "user_review", "possible contradiction with another entry — needs human adjudication"
    if flags.get("broken_pointer"):
        return "verify_current", "references a path that does not exist — verify/repair the pointer"
    if flags.get("duplicate_of") is not None and entry.get("_merge_loser"):
        return "merge", (f"near-duplicate of {flags['duplicate_of']} "
                         f"(jaccard {flags.get('dup_jaccard')}) — merge into the stronger entry")

    if kind == "header":
        return "keep", "navigation header for the notes system"
    if kind == "pointer":
        return "keep", "archived pointer (points to a store; cheapest hot-memory form)"
    if kind == "debugging_finding":
        return "move_to_note", "debugging/root-cause narrative belongs in a note, not hot memory"
    if kind == "status_update":
        if sig["preference"]:  # defense-in-depth: never auto-drop a preference
            return "user_review", "reads as a status update but carries a preference signal — review"
        if has_target or flags.get("paths_referenced"):
            return "archive_to_note", "dated status/event log — archive; leave a pointer if useful"
        return "remove_after_archive", "ephemeral status/metric with no durable home — archive then drop"
    if kind == "project_progress":
        return "verify_current", "dated project progress — confirm still true, then archive to a note"
    if kind == "todo_temporary":
        return "user_review", "TODO/temporary item — confirm done/abandoned, then archive or drop"
    if kind == "content_dump":
        if has_target:
            return "rewrite_to_pointer", "long prose that references a store — collapse to a one-line pointer"
        if re.search(r"\b(how to|steps?|workflow|procedure|recipe|always|never)\b", entry["text"], re.I):
            return "move_to_skill", "procedural how-to content — belongs in a skill, not hot memory"
        return "move_to_note", "long non-pointer knowledge — move to a note and leave a pointer"
    if kind == "malformed":
        return "user_review", "too vague / malformed for a hot pointer — clarify or remove"
    # preference_fact
    if store == "memory" and n > max_entry_chars:
        if sig["preference"]:
            if has_target:
                return "rewrite_to_pointer", "long preference that references a store — collapse to a pointer"
            return "user_review", ("durable preference but long for MEMORY.md — condense in place or "
                                   "move to USER.md (keep it live; do not collapse to a pointer)")
        return "move_to_note", "long non-pointer prose over the per-entry limit — move to a note and leave a pointer"
    if sig["volatile"] and (sig["inline_date"] or sig["temporal"]):
        return "verify_current", "states a volatile current-state fact — verify it still holds"
    return "keep", "durable preference/fact in good hot-memory form"


# --------------------------------------------------------------------------- #
# Cross-entry analysis                                                        #
# --------------------------------------------------------------------------- #
def find_duplicates(entries: list[dict], near: float) -> list[dict]:
    pairs = []
    n = len(entries)
    for i in range(n):
        ei = entries[i]
        if ei["kind"] == "header":
            continue
        for j in range(i + 1, n):
            ej = entries[j]
            if ej["kind"] == "header":
                continue
            if ei["kind"] == "pointer" and ej["kind"] == "pointer":
                continue  # distinct archived pointers share template, not facts
            jac = jaccard(ei["_tokens"], ej["_tokens"])
            if jac >= near:
                cos = cosine_bow(ei["_tokens"], ej["_tokens"])
                pairs.append({"a": ei["ref"], "b": ej["ref"], "jaccard": round(jac, 2),
                              "cosine": round(cos, 2),
                              "strength": "strong" if jac >= DUP_STRONG else "near"})
                _record_dup(ei, ej, jac)
                _record_dup(ej, ei, jac)
                lo = ei if ei["scores"]["overall_quality"] <= ej["scores"]["overall_quality"] else ej
                lo["_merge_loser"] = True
    return pairs


def _record_dup(entry: dict, other: dict, jac: float) -> None:
    cur = entry["flags"].get("dup_jaccard")
    if cur is None or jac > cur:
        entry["flags"]["dup_jaccard"] = round(jac, 2)
        entry["flags"]["duplicate_of"] = other["ref"]


# --------------------------------------------------------------------------- #
# Semantic near-duplicate detection (INTEG-9; optional, daemon-backed)        #
# --------------------------------------------------------------------------- #
def _vec_cosine(a, b) -> float:
    """Cosine similarity between two embedding vectors (robust to non-unit input)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def find_semantic_duplicates(entries: list[dict], embed_fn, *,
                             prefilter_jaccard: float = 0.30,
                             near_cosine: float = 0.85,
                             token_near: float = DUP_NEAR) -> dict | None:
    """Embedding-backed near-duplicate detection. ENHANCEMENT over token Jaccard,
    run only when the caller passes an embedder (memory_audit --semantic).

    Returns ``None`` to signal *embedder unavailable* so the caller falls back to
    token Jaccard only. Otherwise returns a report dict.

    Algorithm (conservative + cheap):
      1. Pre-filter pairs by token Jaccard > ``prefilter_jaccard`` — keeps the embed
         batch and the O(n²) cosine small, and avoids flagging unrelated short
         entries. (pointer/pointer pairs are skipped: they share a template, not a
         fact — same exclusion as the token pass.)
      2. Embed every involved entry's text in ONE daemon round-trip.
      3. Report each candidate pair with cosine >= ``near_cosine`` as a semantic
         near-duplicate. ``token_flagged`` marks pairs the token pass already caught
         (jaccard >= ``token_near``); ``added_over_token`` counts the genuinely new
         finds (the value the semantic tier adds).
    """
    cand = [e for e in entries if e["kind"] != "header"]
    pairs_idx = []
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            if cand[i]["kind"] == "pointer" and cand[j]["kind"] == "pointer":
                continue
            jac = jaccard(cand[i]["_tokens"], cand[j]["_tokens"])
            if jac > prefilter_jaccard:
                pairs_idx.append((i, j, round(jac, 2)))
    base = {"available": True, "checked_pairs": len(pairs_idx), "embedded_entries": 0,
            "near_cosine": near_cosine, "prefilter_jaccard": prefilter_jaccard,
            "pairs": [], "added_over_token": 0}
    if not pairs_idx:
        return base
    involved = sorted({idx for p in pairs_idx for idx in p[:2]})
    vecs = embed_fn([cand[i]["text"] for i in involved])
    if vecs is None:          # daemon down / unsupported -> caller falls back
        return None
    vec_by_idx = {idx: vecs[k] for k, idx in enumerate(involved)}
    out_pairs = []
    for i, j, jac in pairs_idx:
        cos = _vec_cosine(vec_by_idx[i], vec_by_idx[j])
        if cos >= near_cosine:
            out_pairs.append({"a": cand[i]["ref"], "b": cand[j]["ref"],
                              "cosine": round(cos, 3), "jaccard": jac,
                              "token_flagged": jac >= token_near})
    out_pairs.sort(key=lambda d: -d["cosine"])
    base["embedded_entries"] = len(involved)
    base["pairs"] = out_pairs
    base["added_over_token"] = sum(1 for p in out_pairs if not p["token_flagged"])
    return base


def _subject_tokens(text: str, owner_stop: frozenset = frozenset()) -> set[str]:
    return (set(re.findall(r"[a-z][a-z0-9]{3,}", text.lower())) - STOPWORDS - owner_stop)


def _identifiers(text: str) -> set[str]:
    return {m.lower() for m in IDENTIFIER_RE.findall(text)}


def _declared_defaults(text: str) -> set[str]:
    has_default = "default" in text.lower()
    vals: set[str] = set()
    for m in DEFAULT_DECL_RE.findall(text):
        vals.add(m.lower())
    for m in DEFAULT_DECL_RE2.findall(text):
        vals.add(m.lower())
    for m in DEFAULT_AS_RE.findall(text):
        vals.add(m.lower())
    if has_default:
        for m in DEFAULT_TARGET_RE.findall(text):
            vals.add(m.lower())
    return {v.rstrip(".") for v in vals if v} - STOPWORDS


def _contra_signals(entry: dict, owner_stop: frozenset) -> dict:
    """Precompute (ONCE per entry) the four per-entry signals that
    find_contradictions compares pairwise — subject tokens, declared defaults,
    salient identifiers, and polarity flags.

    PERF-1: the previous O(n²) loop recomputed all of these from entry['text']
    for every pair, so each entry was re-tokenised O(n) times (the same waste
    find_duplicates already avoids with its `_tokens` precompute). Hoisting them
    here makes the work O(n) once + O(n²) cheap set comparisons. Reuses any
    signal the caller already attached (e.g. an audit_file precompute or a
    benchmark harness), else derives it — identical output either way."""
    text = entry["text"]
    declared = entry["_declared_defaults"] if "_declared_defaults" in entry else _declared_defaults(text)
    return {
        "subject": entry["_subject_tokens"] if "_subject_tokens" in entry else _subject_tokens(text, owner_stop),
        "defaults": declared - owner_stop,
        "ids": entry["_identifiers"] if "_identifiers" in entry else _identifiers(text),
        "pos": entry["_has_pos"] if "_has_pos" in entry else bool(POLARITY_POS.search(text)),
        "neg": entry["_has_neg"] if "_has_neg" in entry else bool(POLARITY_NEG.search(text)),
    }


def find_contradictions(entries: list[dict], owner_stop: frozenset = frozenset()) -> list[dict]:
    """Conservative: only obvious default-vs-default and (strong same-subject)
    enabled-vs-disabled conflicts. Differing version/subsystem identifiers mean
    different subjects (complementary, not contradictory). Labelled 'possible'.

    PERF-1: per-entry signals are precomputed once (see _contra_signals) instead
    of being re-derived inside the pair loop — a large speedup at scale with
    byte-identical output."""
    out = []
    cand = [e for e in entries if e["kind"] != "header"]
    sig = [_contra_signals(e, owner_stop) for e in cand]   # ONE pass, not O(n²)
    for i in range(len(cand)):
        si = sig[i]
        if not si["subject"]:
            continue
        for j in range(i + 1, len(cand)):
            sj = sig[j]
            if not sj["subject"]:
                continue
            overlap = jaccard(si["subject"], sj["subject"])
            if overlap < CONTRA_SUBJECT_MIN:
                continue
            reason = None
            # Default-vs-default: the differing values ARE the conflict, so no
            # identifier-skip here.
            da, db = si["defaults"], sj["defaults"]
            if da and db and not (da & db):
                reason = f"different 'default' declared ({sorted(da)} vs {sorted(db)}) for an overlapping subject"
            # Enabled-vs-disabled: requires STRONG same-subject overlap, and
            # differing salient identifiers (v2 vs v3, mt5 vs ...) mean the two
            # entries are about DIFFERENT things (complementary, not conflicting).
            if reason is None and overlap >= CONTRA_POLARITY_MIN:
                ida, idb = si["ids"], sj["ids"]
                if not (ida and idb and not (ida & idb)):
                    if (si["pos"] and sj["neg"] and not si["neg"] and not sj["pos"]) or \
                       (si["neg"] and sj["pos"] and not si["pos"] and not sj["neg"]):
                        reason = "opposing state (enabled/active vs paused/disabled/removed) on the same subject"
            if reason:
                a, b = cand[i], cand[j]
                out.append({"a": a["ref"], "b": b["ref"], "subject_overlap": round(overlap, 2),
                            "reason": reason})
                # setdefault so a bare entry dict (e.g. a benchmark harness without
                # the audit_file 'flags' scaffold) never KeyErrors; real audit
                # entries already carry flags, so this is a no-op for them.
                a.setdefault("flags", {})["possible_contradiction"] = b["ref"]
                b.setdefault("flags", {})["possible_contradiction"] = a["ref"]
    return out


# --------------------------------------------------------------------------- #
# File + whole-audit assembly                                                 #
# --------------------------------------------------------------------------- #
def audit_file(path: str, store: str, user_home: str, *, max_entry_chars: int,
               today: _dt.date, stale_days: int, owner_stop: frozenset) -> dict:
    raw_entries = parse_entries(path)
    char_limit = DEFAULT_MEMORY_CHAR_LIMIT if store == "memory" else DEFAULT_USER_CHAR_LIMIT
    total_chars = os.path.getsize(path) if (path and os.path.exists(path)) else 0
    out_entries = []
    for e in raw_entries:
        text = e["text"]
        n = len(text)
        sig = signals_for(text)
        kind = classify_kind(text, store, sig, n, max_entry_chars)
        paths = extract_paths(text)
        file_paths = [p for p in paths if is_file_path(p)]
        missing = [p for p in file_paths if not os.path.exists(resolve_path(p, user_home))]
        paths_missing = bool(file_paths) and len(missing) == len(file_paths) and kind != "header"
        has_real_target = bool(file_paths) or sig["pointer_sigil"]
        scores = score_entry(text, store, sig, n, kind, paths_missing, today, stale_days)
        ref = f"{store}#{e['index']}"
        flags = {
            "too_long": n > max_entry_chars and kind not in ("header",),
            "too_short": n < 25,
            "paths_referenced": bool(paths),
            "paths_missing": missing,
            "broken_pointer": paths_missing,
            "has_real_target": has_real_target,
            "volatile_claim": sig["volatile"],
            "dup_jaccard": None,
            "duplicate_of": None,
            "possible_contradiction": None,
        }
        out_entries.append({
            "ref": ref, "store": store, "index": e["index"], "chars": n,
            "preview": first_line(text)[:100],
            "text": text, "key": _derive_key(text), "kind": kind,
            "signals": sig, "flags": flags, "scores": scores,
            "dates": [d.isoformat() for d in extract_dates(text)],
            "paths_referenced": paths,
            "_tokens": norm_tokens(text, owner_stop),
        })
    return {
        "path": path, "store": store, "exists": bool(path and os.path.exists(path)),
        "sha256": sha256_text(read_text(path)) if (path and os.path.exists(path)) else None,
        "char_count": total_chars, "char_limit": char_limit,
        "capacity_pct": round(100 * total_chars / char_limit, 1) if char_limit else None,
        "entry_count": len(out_entries),
        "entries": out_entries,
    }


def _derive_key(text: str) -> str:
    if is_header(text):
        return "notes-system-header"
    base = first_line(text)
    base = base[1:].strip() if base.startswith(POINTER_SIGIL) else base
    m = re.match(r"^([A-Za-z][\w &/+.\-]{1,46}?)\s*(?:\([^)]*\))?\s*:", base)
    if m:
        slug = re.sub(r"[^a-z0-9]+", "-", m.group(1).lower()).strip("-")
        if slug and slug not in {"user", "note", "memory", "update", "todo", "wip"}:
            return slug[:48]
    toks = [t for t in re.findall(r"[a-z][a-z0-9]{2,}", base.lower()) if t not in STOPWORDS][:6]
    s = re.sub(r"[^a-z0-9]+", "-", " ".join(toks)).strip("-")
    return (s[:48] or "entry")


def run_audit(memory_path: str, user_path: str, home: str, *,
              max_entry_chars: int = DEFAULT_MAX_ENTRY_CHARS, strict: bool = False,
              today: _dt.date | None = None,
              stale_days: int = DEFAULT_STALE_AFTER_DAYS,
              user_home: str | None = None, owner_name: str | None = None,
              semantic_embed=None, semantic_cosine: float = 0.85,
              semantic_prefilter: float = 0.30) -> dict:
    today = today or _dt.date.today()
    user_home = user_home or os.path.expanduser("~")
    owner_stop = owner_stopwords(user_home, owner_name)
    near = STRICT_DUP_NEAR if strict else DUP_NEAR
    if strict:
        max_entry_chars = min(max_entry_chars, STRICT_MAX_ENTRY_CHARS)

    files = []
    for p, store in ((memory_path, "memory"), (user_path, "user")):
        if p:
            files.append(audit_file(p, store, user_home, max_entry_chars=max_entry_chars,
                                    today=today, stale_days=stale_days, owner_stop=owner_stop))

    all_entries = [e for f in files for e in f["entries"]]
    duplicates = find_duplicates(all_entries, near)
    contradictions = find_contradictions(all_entries, owner_stop)

    # INTEG-9: optional embedding-backed near-dup pass (needs entries' _tokens/text,
    # so it runs BEFORE the _tokens pop below). None => embedder unavailable.
    semantic = None
    if semantic_embed is not None:
        sem = find_semantic_duplicates(all_entries, semantic_embed,
                                       prefilter_jaccard=semantic_prefilter,
                                       near_cosine=semantic_cosine, token_near=near)
        semantic = sem if sem is not None else {
            "available": False, "pairs": [],
            "reason": "semantic daemon unavailable — token Jaccard only"}

    for e in all_entries:
        action, rationale = recommend(e, e["store"], max_entry_chars)
        e["recommended_action"] = action
        e["rationale"] = rationale

    summary = _summarize(files, all_entries, duplicates, contradictions)
    for e in all_entries:
        e.pop("_tokens", None)
        e.pop("_merge_loser", None)
    report = {
        "tool": "memory_audit", "tool_version": TOOL_VERSION,
        "generated_at": today.isoformat(), "home": home,
        "params": {"max_entry_chars": max_entry_chars, "strict": strict,
                   "stale_after_days": stale_days, "dup_near_threshold": near,
                   "owner_stopwords": sorted(owner_stop), "user_home": user_home},
        "files": files, "duplicate_pairs": duplicates,
        "contradiction_pairs": contradictions, "summary": summary,
    }
    if semantic is not None:
        report["semantic_duplicates"] = semantic
    return report


def _est_saved(e: dict) -> int | None:
    r = _ACTION_RESIDUAL.get(e["recommended_action"])
    return None if r is None else max(0, e["chars"] - r)


def _summarize(files, all_entries, duplicates, contradictions) -> dict:
    by_kind, by_action = {}, {}
    for e in all_entries:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        by_action[e["recommended_action"]] = by_action.get(e["recommended_action"], 0) + 1
    actionable = [e for e in all_entries if e["recommended_action"] != "keep"]
    broken = [e["ref"] for e in all_entries if e["flags"]["broken_pointer"]]
    worst = sorted(all_entries, key=lambda e: e["scores"]["overall_quality"])[:10]

    # Biggest shrink wins (chars-ranked, only entries whose action frees space).
    shrinkable = [e for e in all_entries if _est_saved(e) is not None and _est_saved(e) > 0]
    shrinkable.sort(key=lambda e: _est_saved(e), reverse=True)
    top_shrink = [{"ref": e["ref"], "kind": e["kind"], "action": e["recommended_action"],
                   "chars": e["chars"], "est_saved": _est_saved(e)} for e in shrinkable[:10]]

    cap = {}
    savings = {}
    for f in files:
        flag = "ok"
        if f["capacity_pct"] is not None:
            if f["capacity_pct"] >= CAPACITY_CRIT_PCT:
                flag = "CRITICAL"
            elif f["capacity_pct"] >= CAPACITY_WARN_PCT:
                flag = "WARNING"
        cap[f["store"]] = {"chars": f["char_count"], "limit": f["char_limit"],
                           "pct": f["capacity_pct"], "flag": flag, "entries": f["entry_count"]}
        rec = sum(_est_saved(e) for e in f["entries"] if _est_saved(e) is not None)
        savings[f["store"]] = {
            "est_recoverable_chars": rec,
            "projected_chars_after": max(0, f["char_count"] - rec),
            "projected_pct": round(100 * max(0, f["char_count"] - rec) / f["char_limit"], 1)
            if f["char_limit"] else None,
        }

    mem = next((f for f in files if f["store"] == "memory"), None)
    entry_pressure = None
    if mem:
        entry_pressure = {"count": mem["entry_count"], "target": DEFAULT_ENTRY_TARGET,
                          "ceiling": DEFAULT_ENTRY_CEILING,
                          "over_target": mem["entry_count"] > DEFAULT_ENTRY_TARGET,
                          "over_ceiling": mem["entry_count"] > DEFAULT_ENTRY_CEILING}
    return {
        "total_entries": len(all_entries),
        "by_kind": by_kind, "by_recommended_action": by_action,
        "actionable_entries": len(actionable),
        "keep_entries": len(all_entries) - len(actionable),
        "duplicate_pairs": len(duplicates), "contradiction_pairs": len(contradictions),
        "broken_pointers": broken, "capacity": cap, "estimated_savings": savings,
        "entry_pressure": entry_pressure, "top_shrink_targets": top_shrink,
        "lowest_quality_refs": [{"ref": e["ref"], "kind": e["kind"],
                                 "action": e["recommended_action"],
                                 "quality": e["scores"]["overall_quality"],
                                 "preview": e["preview"]} for e in worst],
    }


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #
def render_markdown(report: dict) -> str:
    L = ["# Hermes Hot-Memory Audit (read-only)", "",
         f"_Generated {report['generated_at']} · tool v{report['tool_version']} · "
         f"strict={report['params']['strict']} · max_entry_chars={report['params']['max_entry_chars']}_"]
    s = report["summary"]
    L += ["", "## Capacity", "", "| Store | Entries | Chars | Limit | Capacity | Flag | Recoverable | Projected |",
          "|---|---:|---:|---:|---:|---|---:|---:|"]
    for store, c in s["capacity"].items():
        sv = s["estimated_savings"].get(store, {})
        L.append(f"| {store} | {c['entries']} | {c['chars']} | {c['limit']} | {c['pct']}% | {c['flag']} | "
                 f"~{sv.get('est_recoverable_chars', 0)} | {sv.get('projected_chars_after', '?')} "
                 f"({sv.get('projected_pct', '?')}%) |")
    ep = s.get("entry_pressure")
    if ep:
        L += ["", f"MEMORY.md entries: **{ep['count']}** (target {ep['target']}, ceiling {ep['ceiling']})"
              + ("  ⚠️ **over ceiling**" if ep["over_ceiling"]
                 else "  ⚠️ over target" if ep["over_target"] else "  ✓")]
    L += ["", "## Summary", "",
          f"- Total entries: **{s['total_entries']}**  ·  keep: {s['keep_entries']}  ·  "
          f"actionable: **{s['actionable_entries']}**",
          "- By kind: " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_kind"].items())),
          "- Recommended actions: " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_recommended_action"].items())),
          f"- Duplicate pairs: {s['duplicate_pairs']}  ·  Possible contradictions: {s['contradiction_pairs']}  ·  "
          f"Broken pointers: {len(s['broken_pointers'])}"]
    if s["top_shrink_targets"]:
        L += ["", "## Biggest shrink targets (most chars recoverable)", "",
              "| Ref | Chars | Action | Est. saved |", "|---|---:|---|---:|"]
        for t in s["top_shrink_targets"]:
            L.append(f"| {t['ref']} | {t['chars']} | {t['action']} | ~{t['est_saved']} |")
    if report["duplicate_pairs"]:
        L += ["", "## Near-duplicate pairs (consolidate)", ""]
        for d in report["duplicate_pairs"]:
            L.append(f"- `{d['a']}` ⇄ `{d['b']}` — jaccard {d['jaccard']} ({d['strength']})")
    if report["contradiction_pairs"]:
        L += ["", "## Possible contradictions (human review — NOT asserted as fact)", ""]
        for c in report["contradiction_pairs"]:
            L.append(f"- `{c['a']}` vs `{c['b']}` — {c['reason']}")
    sem = report.get("semantic_duplicates")
    if sem is not None:
        if not sem.get("available", False):
            L += ["", "## Semantic near-duplicates", "",
                  f"_{sem.get('reason', 'semantic daemon unavailable')}; token Jaccard used._"]
        else:
            L += ["", "## Semantic near-duplicates (embedding cosine ≥ "
                  f"{sem.get('near_cosine')})", ""]
            if sem["pairs"]:
                for d in sem["pairs"]:
                    extra = "" if d["token_flagged"] else "  ← missed by token Jaccard"
                    L.append(f"- `{d['a']}` ⇄ `{d['b']}` — cosine {d['cosine']} "
                             f"(jaccard {d['jaccard']}){extra}")
                L.append(f"\n_{sem['checked_pairs']} candidate pair(s) checked; "
                         f"{sem['added_over_token']} beyond token Jaccard._")
            else:
                L.append(f"_None (checked {sem['checked_pairs']} candidate pair(s))._")
    if s["broken_pointers"]:
        L += ["", "## Broken pointers (referenced file missing)", "",
              "- " + ", ".join(f"`{r}`" for r in s["broken_pointers"])]
    L += ["", "## Per-entry findings"]
    for f in report["files"]:
        if not f["exists"]:
            L += ["", f"### {f['store']} — `{f['path']}` (not found)"]
            continue
        L += ["", f"### {f['store']} — `{f['path']}`", "",
              "| Ref | Kind | Action | Quality | Chars | Flags | Preview |", "|---|---|---|---:|---:|---|---|"]
        for e in f["entries"]:
            preview = e["preview"].replace("|", "\\|")
            L.append(f"| {e['ref']} | {e['kind']} | **{e['recommended_action']}** | "
                     f"{e['scores']['overall_quality']} | {e['chars']} | {_flag_str(e['flags'])} | {preview} |")
    L += ["", "---",
          "_Read-only audit. No files were modified. Area 3 (pointer rewrite) acts on this report._"]
    return "\n".join(L)


def _flag_str(flags: dict) -> str:
    parts = []
    if flags.get("too_long"):
        parts.append("long")
    if flags.get("broken_pointer"):
        parts.append("broken-path")
    if flags.get("duplicate_of"):
        parts.append(f"dup~{flags.get('dup_jaccard')}")
    if flags.get("possible_contradiction"):
        parts.append("contradiction?")
    if flags.get("volatile_claim"):
        parts.append("volatile")
    return ", ".join(parts) or "—"


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def _default_paths(home: str) -> tuple[str, str]:
    return (os.path.join(home, "memories", "MEMORY.md"),
            os.path.join(home, "memories", "USER.md"))


def _canon(p: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(p)))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memory_audit.py",
        description="Read-only quality audit of Hermes hot memory (MEMORY.md / USER.md).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="READ-ONLY: never modifies MEMORY.md/USER.md. The only write is --out, "
               "and the tool refuses if --out resolves to an input file.\n\n"
               "PIPELINE (Area 2 -> Area 3): this is step 4 of RUNBOOK.md.\n"
               "  this:  memory_audit.py --home ~/.hermes --json --out /tmp/mem-audit.json\n"
               "  next:  memory_rewrite.py render --audit /tmp/mem-audit.json --out-dir /tmp/proposed")
    p.add_argument("--home", help="Hermes home (default $HERMES_HOME or ~/.hermes); locates default files")
    p.add_argument("--user-home", help="OS home dir used to resolve ~/ paths (default real $HOME; "
                   "its basename also seeds the owner-name stopword)")
    p.add_argument("--owner-name", help="owner name token(s) to ignore for dup/contradiction "
                   "(default: basename of --user-home)")
    p.add_argument("--memory", help="path to MEMORY.md (default <home>/memories/MEMORY.md)")
    p.add_argument("--user", help="path to USER.md (default <home>/memories/USER.md)")
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--markdown", action="store_true", help="emit markdown (default)")
    p.add_argument("--out", help="write the report to this path (refuses an input file)")
    p.add_argument("--max-entry-chars", type=int, default=DEFAULT_MAX_ENTRY_CHARS,
                   help=f"flag entries longer than this (default {DEFAULT_MAX_ENTRY_CHARS})")
    p.add_argument("--strict", action="store_true", help="tighter dup/length thresholds")
    p.add_argument("--stale-after-days", type=int, default=DEFAULT_STALE_AFTER_DAYS)
    p.add_argument("--semantic", action="store_true",
                   help="ALSO run embedding-backed near-duplicate detection via the semantic "
                        "daemon (INTEG-9). Enhancement only; falls back to token Jaccard with a "
                        "warning if the daemon is not running. Adds a 'semantic_duplicates' field.")
    p.add_argument("--semantic-cosine", type=float, default=0.85,
                   help="cosine threshold for a semantic near-duplicate (default 0.85)")
    p.add_argument("--semantic-prefilter", type=float, default=0.30,
                   help="only embed/compare pairs with token Jaccard above this (default 0.30)")
    p.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    home = os.path.abspath(os.path.expanduser(
        args.home or os.environ.get("HERMES_HOME") or "~/.hermes"))
    def_mem, def_usr = _default_paths(home)
    memory_path = os.path.expanduser(args.memory) if args.memory else def_mem
    user_path = os.path.expanduser(args.user) if args.user else def_usr
    user_home = os.path.abspath(os.path.expanduser(args.user_home)) if args.user_home \
        else os.path.expanduser("~")

    # Refuse to write the report onto an input hot file (no clobber; resolves
    # ~/-vs-absolute aliases and symlinks).
    if args.out:
        out_real = _canon(args.out)
        if out_real in {_canon(memory_path), _canon(user_path)}:
            print(f"refusing to write report onto an input hot-memory file: {args.out}", file=sys.stderr)
            return 2

    if not os.path.exists(memory_path) and not os.path.exists(user_path):
        print(f"no hot-memory files found at {memory_path} or {user_path}", file=sys.stderr)

    # INTEG-9: build a daemon-backed embedder only when --semantic is requested.
    # Import is lazy + failure-tolerant; embed_texts itself returns None when the
    # daemon is down, so run_audit cleanly degrades to token Jaccard.
    semantic_embed = None
    if args.semantic:
        try:
            import semantic_query as _SQ  # lazy: only on the --semantic path
            semantic_embed = _SQ.embed_texts
        except Exception as e:
            print(f"[semantic] semantic_query unavailable ({e}); token Jaccard only", file=sys.stderr)
            semantic_embed = (lambda _texts: None)

    report = run_audit(memory_path, user_path, home,
                       max_entry_chars=args.max_entry_chars, strict=args.strict,
                       stale_days=args.stale_after_days, user_home=user_home,
                       owner_name=args.owner_name, semantic_embed=semantic_embed,
                       semantic_cosine=args.semantic_cosine,
                       semantic_prefilter=args.semantic_prefilter)

    if args.semantic:
        sd = report.get("semantic_duplicates", {})
        if not sd.get("available", False):
            print(f"[semantic] {sd.get('reason', 'daemon unavailable')} — is the daemon running? "
                  f"(python3 semantic_query.py --ping)", file=sys.stderr)
        else:
            print(f"[semantic] {sd.get('checked_pairs', 0)} candidate pair(s) checked; "
                  f"{len(sd.get('pairs', []))} semantic near-dup(s), "
                  f"{sd.get('added_over_token', 0)} beyond token Jaccard", file=sys.stderr)

    as_json = args.json and not args.markdown
    if args.out:
        if args.out.endswith(".json"):
            as_json = True
        elif args.out.endswith(".md"):
            as_json = False
    text = json.dumps(report, indent=2, default=str) if as_json else render_markdown(report)
    if args.out:
        # Create the parent dir so `--out /tmp/new/report.json` just works (matches
        # memory_rewrite render's --out-dir) instead of a FileNotFoundError traceback.
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"[wrote report] {args.out}  ({'json' if as_json else 'markdown'})", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
