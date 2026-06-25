#!/usr/bin/env python3
"""Cheap dynamic search map — the agent's first-pass router for "where does the
knowledge I'm missing actually live?".

When the current prompt context lacks what the agent needs, going deeper should
stay CHEAP. Before spraying expensive semantic/vector/session reads, the agent
reads this tiny map (<= ~1000 tokens), sees which stores are healthy/fresh and
what topics live where, then routes straight to the right SEARCH LANE using
relevance (lexical topic match), recency/time, semantic retrieval, temporal
history, notes, and source code.

Design note: references/cheap-dynamic-search-map-2026-06-25.md. This is the
"future routing layer before retrieval/projection" promised there — it sits in
front of memory_project.py (projection), memory_entry_index.py / semantic_query.py
(retrieval), and temporal_memory.py (history), and it tells the agent which of
those to reach for first.

THREE COMMANDS
    build   — emit the compact map from the current stores (JSON, or --markdown).
    query   — given --query, rank the search lanes and print the exact next
              commands (relevance + intent heuristics over the map).
    doctor  — validate store freshness/health; report missing/stale stores and a
              PASS/WARN/FAIL verdict (exit 0 unless a required store is missing
              or --strict and WARN).

CHEAP + SAFE BY DESIGN
    * stdlib only. No LLM, no embedding-model load, no ChromaDB import. The only
      semantic touch is a pure-stdlib socket PING to the warm daemon (short
      timeout, degrades to "daemon-down" if absent).
    * READ-ONLY. It never mutates MEMORY.md / USER.md / notes / any DB. SQLite is
      opened in immutable read-only mode (file:...?mode=ro). The temporal layer is
      read directly from its derived index, NOT via TemporalMemory() (whose ctor
      rebuilds/writes).
    * NEVER emits raw memory bodies or secrets. Topics are reduced to a short,
      secret-scrubbed label + a kebab key + a handful of match terms. Every string
      that reaches output passes through a secret redactor.
    * Graceful: a missing/locked optional store becomes a degraded lane, never a
      crash.

Run:
    python3 scripts/memory_search_map.py build  --home ~/.hermes --json
    python3 scripts/memory_search_map.py build  --home ~/.hermes --markdown
    python3 scripts/memory_search_map.py query  --home ~/.hermes --query "semantic retrieval shadow mode" --json
    python3 scripts/memory_search_map.py doctor --home ~/.hermes --json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# memory_audit owns entry parsing + the audited scoring dimensions; reuse it so
# the map's topic view matches the rest of the stack (one source of truth).
import memory_audit as MA  # noqa: E402

TOOL_VERSION = "1.0.0"

# Token model — same chars/4 estimate the projection/shadow layers use, so the
# "<= 1000 token" budget is measured on the same ruler as everything else.
CHARS_PER_TOKEN = 4
DEFAULT_MAP_TOKEN_BUDGET = 1000

# Freshness thresholds (days). A store older than its threshold is "stale" — a
# WARN-level signal in doctor, and a small ranking penalty in query.
DEFAULT_FRESH_DAYS = {
    "hot_memory": 30,
    "user_memory": 45,
    "notes_index": 21,
    "master_index": 45,
    "temporal": 30,
    "spine": 21,
    "shadow_report": 7,
}

# How many topics to surface in the map (keeps the markdown under budget). Query
# matching scans the full topic set in-process; only the emitted list is capped.
DEFAULT_MAX_TOPICS = 24
MAX_TERMS_PER_TOPIC = 8
LABEL_MAX = 60

_WORD_RE = re.compile(r"[a-z][a-z0-9_]{2,}")
_QUERY_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "via", "are",
    "was", "now", "not", "but", "you", "use", "uses", "used", "per", "out",
    "all", "any", "has", "have", "how", "did", "does", "what", "when", "where",
    "which", "who", "why", "our", "your", "their", "about", "get", "got", "set",
    "can", "will", "should", "would", "could", "did", "find", "search", "show",
    "tell", "give", "need", "want", "look", "see", "read", "make", "run",
}

# --------------------------------------------------------------------------- #
# Secret redaction — defence in depth. Topic labels come from the FIRST LINE of  #
# a memory entry, which could in principle contain a pasted credential. Every    #
# string that reaches output is scrubbed before emission.                        #
# --------------------------------------------------------------------------- #
_SECRET_RES = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # generic "<secret-word>: <value>" / "<secret-word>=<value>"
    re.compile(r"(?i)\b(api[_-]?key|secret|client[_-]?secret|access[_-]?token|"
               r"refresh[_-]?token|password|passwd|bearer)\b\s*[:=]\s*\S+"),
    # long opaque hex/base64 runs (32+); 16-hex content hashes survive on purpose
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
]


def redact(text: str) -> str:
    """Mask anything that looks like a credential/opaque token. Conservative on
    false positives (a long hash becomes [REDACTED]) — the map never needs the
    literal value, only the topic."""
    if not text:
        return ""
    out = text
    for rx in _SECRET_RES:
        out = rx.sub("[REDACTED]", out)
    return out


def est_tokens(text: str) -> int:
    n = len(text or "")
    if n <= 0:
        return 0
    return (n + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def clean_label(text: str, limit: int = LABEL_MAX) -> str:
    """A short, single-line, secret-scrubbed label for a topic."""
    s = redact((text or "").strip().splitlines()[0] if (text or "").strip() else "")
    s = s.strip(" -*#`").replace("`", "")
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def query_terms(text: str) -> list[str]:
    """Lowercase content words from a query/topic, minus a small stoplist."""
    return [t for t in _WORD_RE.findall((text or "").lower()) if t not in _QUERY_STOP]


# --------------------------------------------------------------------------- #
# Path / freshness plumbing                                                    #
# --------------------------------------------------------------------------- #
def resolve_home(home: str | None) -> str:
    return os.path.abspath(os.path.expanduser(
        home or os.environ.get("HERMES_HOME") or "~/.hermes"))


def resolve_scripts_dir(home: str, explicit: str | None) -> str:
    """Where the runnable scripts live, for the commands we emit. Prefer the
    installed <home>/scripts, fall back to this package's scripts dir."""
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    cand = os.path.join(home, "scripts")
    if os.path.exists(os.path.join(cand, "memory_entry_index.py")):
        return cand
    return _HERE


def _safe_mtime(path: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromtimestamp(os.path.getmtime(path), _dt.timezone.utc)
    except OSError:
        return None


def _age_days(when: _dt.datetime | None, today: _dt.date) -> int | None:
    if when is None:
        return None
    return max(0, (today - when.date()).days)


def _ro_connect(path: str) -> sqlite3.Connection | None:
    """Open a SQLite DB strictly read-only. Returns None if absent/unopenable.
    Uses immutable=1 so we never create -wal/-shm sidecars or take write locks."""
    if not path or not os.path.exists(path):
        return None
    try:
        uri = f"file:{os.path.abspath(path)}?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _scalar(con: sqlite3.Connection, sql: str, default=None):
    try:
        row = con.execute(sql).fetchone()
        return row[0] if row and row[0] is not None else default
    except sqlite3.Error:
        return default


# --------------------------------------------------------------------------- #
# Store probes — each returns a compact health dict. All degrade gracefully.    #
# --------------------------------------------------------------------------- #
def probe_hot_memory(home: str, today: _dt.date, fresh_days: dict) -> tuple[dict, dict, list[dict]]:
    """Audit MEMORY.md + USER.md (cheap, stdlib) → (memory_health, user_health,
    topics). Topics are reduced to label/key/terms — NEVER full bodies."""
    user_home = os.path.expanduser("~")
    owner_stop = MA.owner_stopwords(user_home)
    mem_path, usr_path = MA._default_paths(home)
    healths: dict[str, dict] = {}
    topics: list[dict] = []
    for path, store, fkey, lane in (
        (mem_path, "memory", "hot_memory", "memory-entry"),
        (usr_path, "user", "user_memory", "memory-entry"),
    ):
        exists = bool(path and os.path.exists(path))
        h = {
            "present": exists,
            "path": _tilde(path),
            "entries": 0,
            "chars": 0,
            "capacity_pct": None,
            "age_days": None,
            "fresh": None,
            "latest_dated": None,
        }
        if not exists:
            h["status"] = "missing"
            healths[fkey] = h
            continue
        try:
            af = MA.audit_file(path, store, user_home,
                               max_entry_chars=MA.DEFAULT_MAX_ENTRY_CHARS,
                               today=today, stale_days=MA.DEFAULT_STALE_AFTER_DAYS,
                               owner_stop=owner_stop)
        except Exception as e:  # pragma: no cover - defensive
            h["status"] = f"unreadable:{type(e).__name__}"
            healths[fkey] = h
            continue
        h["entries"] = af["entry_count"]
        h["chars"] = af["char_count"]
        h["capacity_pct"] = af["capacity_pct"]
        mt = _safe_mtime(path)
        h["age_days"] = _age_days(mt, today)
        h["fresh"] = (h["age_days"] is not None and h["age_days"] <= fresh_days.get(fkey, 30))
        # latest in-text date across entries (cheap recency proxy, no temporal load)
        latest = None
        for e in af["entries"]:
            d = MA.latest_date(e["text"])
            if d and (latest is None or d > latest):
                latest = d
        h["latest_dated"] = latest.isoformat() if latest else None
        h["status"] = "ok"
        healths[fkey] = h
        # build topics with salience for ranking the emitted slice
        for e in af["entries"]:
            sc = e.get("scores") or {}
            salience = float(sc.get("durability", 0.0)) + float(sc.get("specificity_actionability", 0.0))
            terms = sorted(set(t for t in e.get("_tokens", set()) if t not in _QUERY_STOP))
            topics.append({
                "label": clean_label(e["preview"]),
                "key": e["key"],
                "source": fkey,
                "lane": lane,
                "where": _tilde(path),
                "terms": terms,
                "_salience": round(salience, 3),
            })
    return healths["hot_memory"], healths["user_memory"], topics


_NOTE_BULLET_RE = re.compile(r"^\s*[-*]\s+`?([^`\s][^`]*?\.md)`?\s*[—:-]\s*(.*)$")
_NOTE_HEADING_RE = re.compile(r"^#{2,4}\s+(.+?)\s*$")


def probe_notes(home: str, today: _dt.date, fresh_days: dict,
                notes_dir: str | None = None) -> tuple[dict, dict, list[dict]]:
    """Parse notes/INDEX.md + MASTER_CONTEXT_INDEX.md into canonical-path topics.
    Never emits note BODIES — only the index's own table-of-contents lines."""
    nd = notes_dir or os.path.join(home, "notes")
    index_path = os.path.join(nd, "INDEX.md")
    master_path = os.path.join(nd, "MASTER_CONTEXT_INDEX.md")
    topics: list[dict] = []

    def _probe(path: str, fkey: str) -> dict:
        exists = os.path.exists(path)
        mt = _safe_mtime(path)
        age = _age_days(mt, today)
        h = {
            "present": exists,
            "path": _tilde(path),
            "age_days": age,
            "fresh": (age is not None and age <= fresh_days.get(fkey, 30)) if exists else None,
            "topics_indexed": 0,
            "status": "ok" if exists else "missing",
        }
        if not exists:
            return h
        try:
            text = _read_text_safe(path)
        except OSError:
            h["status"] = "unreadable"
            return h
        n = 0
        for line in text.splitlines():
            m = _NOTE_BULLET_RE.match(line)
            if not m:
                continue
            rel, desc = m.group(1).strip(), m.group(2).strip()
            label = clean_label(f"{rel} — {desc}")
            where = rel if rel.startswith(("/", "~")) else _tilde(os.path.join(nd, rel))
            terms = sorted(set(query_terms(rel.replace("/", " ").replace("-", " ")) + query_terms(desc)))
            topics.append({
                "label": label,
                "key": _slug(os.path.splitext(os.path.basename(rel))[0]),
                "source": fkey,
                "lane": "notes-canonical",
                "where": where,
                "terms": terms[:24],
                "_salience": 0.6,
            })
            n += 1
        h["topics_indexed"] = n
        return h

    index_h = _probe(index_path, "notes_index")
    master_h = _probe(master_path, "master_index")
    return index_h, master_h, topics


def probe_semantic(home: str, ping_timeout: float) -> dict:
    """Pure-stdlib socket ping to the warm semantic daemon. NEVER imports Chroma.
    Returns counts for the sessions + memories collections, or a degraded note."""
    sock_path = os.path.join(home, "chroma", "semantic.sock")
    chroma_dir = os.path.join(home, "chroma", "sessions")
    h = {
        "present": os.path.exists(chroma_dir),
        "path": _tilde(chroma_dir),
        "daemon": "down",
        "sessions": None,
        "memories": None,
        "status": "down",
    }
    if not os.path.exists(sock_path):
        h["status"] = "no-socket" if h["present"] else "missing"
        return h
    try:
        import semantic_query as SQ  # lazy; module import does NOT load chroma
        resp = SQ.ping(sock_path=sock_path, timeout=ping_timeout)
    except Exception as e:  # pragma: no cover - environment dependent
        h["status"] = f"ping-error:{type(e).__name__}"
        return h
    if resp.get("ok"):
        counts = resp.get("collection_counts") or {}
        h["daemon"] = "up"
        h["sessions"] = int(counts.get("sessions", resp.get("collection_count", 0)) or 0)
        h["memories"] = int(counts.get("memories", 0) or 0)
        h["status"] = "ok"
    else:
        h["status"] = "ping-failed"
    return h


def probe_temporal(home: str, today: _dt.date, fresh_days: dict,
                   db_path: str | None = None) -> dict:
    """Read the derived temporal index READ-ONLY. Does NOT instantiate
    TemporalMemory (its ctor rebuilds + writes). Counts current facts + newest
    recorded_at for freshness."""
    path = db_path or os.path.join(home, "memory_versions.db")
    h = {"present": os.path.exists(path), "path": _tilde(path),
         "current_facts": None, "total_versions": None, "facts_with_history": None,
         "last_recorded": None, "age_days": None, "fresh": None,
         "status": "missing"}
    con = _ro_connect(path)
    if con is None:
        if h["present"]:
            h["status"] = "unreadable"
        return h
    try:
        h["current_facts"] = _scalar(con, "SELECT COUNT(*) FROM versions WHERE is_current=1", 0)
        h["total_versions"] = _scalar(con, "SELECT COUNT(*) FROM versions", 0)
        h["facts_with_history"] = _scalar(
            con, "SELECT COUNT(*) FROM (SELECT fact_key FROM versions GROUP BY fact_key HAVING COUNT(*)>1)", 0)
        last = _scalar(con, "SELECT MAX(recorded_at) FROM versions")
        h["last_recorded"] = last
        age = _age_days(_parse_iso(last), today)
        h["age_days"] = age
        h["fresh"] = (age is not None and age <= fresh_days.get("temporal", 30))
        h["status"] = "ok"
    finally:
        con.close()
    return h


def probe_spine(home: str, today: _dt.date, fresh_days: dict) -> dict:
    """Memory Spine evidence ledger — present-and-counts only, READ-ONLY. Optional;
    never required."""
    path = os.path.join(home, "memory_spine", "memory_spine.sqlite")
    h = {"present": os.path.exists(path), "path": _tilde(path),
         "events": None, "artifacts": None, "last_event": None,
         "age_days": None, "fresh": None, "status": "missing"}
    con = _ro_connect(path)
    if con is None:
        if h["present"]:
            h["status"] = "unreadable"
        return h
    try:
        h["events"] = _scalar(con, "SELECT COUNT(*) FROM events", 0)
        h["artifacts"] = _scalar(con, "SELECT COUNT(*) FROM artifacts", 0)
        # events table has a timestamp-ish column; probe common names defensively
        last = None
        for col in ("ts", "created_at", "recorded_at", "occurred_at", "time", "timestamp"):
            last = _scalar(con, f"SELECT MAX({col}) FROM events")
            if last:
                break
        h["last_event"] = str(last) if last is not None else None
        age = _age_days(_parse_iso(str(last)) if last else None, today)
        h["age_days"] = age
        h["fresh"] = (age is not None and age <= fresh_days.get("spine", 30)) if age is not None else None
        h["status"] = "ok"
    finally:
        con.close()
    return h


def probe_shadow_report(home: str, today: _dt.date, fresh_days: dict,
                        reports_dirs: list[str] | None = None) -> dict:
    """Newest shadow-report-*.json across candidate report dirs → latest PASS/
    WARN/FAIL. Cheap JSON read; no telemetry recompute."""
    cands = reports_dirs or [
        os.path.join(home, "reports"),
        os.path.join(home, "packages", "hermes-memory-stack", "reports"),
        os.path.join(_HERE, "..", "reports"),
    ]
    newest_path, newest_mt = None, -1.0
    for d in cands:
        d = os.path.abspath(os.path.expanduser(d))
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not (name.startswith("shadow-report") and name.endswith(".json")):
                continue
            p = os.path.join(d, name)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt > newest_mt:
                newest_path, newest_mt = p, mt
    h = {"present": newest_path is not None, "path": _tilde(newest_path) if newest_path else None,
         "report_status": None, "generated_at": None, "age_days": None,
         "fresh": None, "status": "missing"}
    if not newest_path:
        return h
    try:
        data = json.loads(_read_text_safe(newest_path))
    except Exception:
        h["status"] = "unreadable"
        return h
    h["report_status"] = data.get("status")
    h["generated_at"] = data.get("generated_at")
    age = _age_days(_dt.datetime.fromtimestamp(newest_mt, _dt.timezone.utc), today)
    h["age_days"] = age
    h["fresh"] = (age is not None and age <= fresh_days.get("shadow_report", 7))
    h["status"] = "ok"
    return h


# --------------------------------------------------------------------------- #
# Small shared helpers                                                         #
# --------------------------------------------------------------------------- #
def _tilde(path: str | None) -> str | None:
    if not path:
        return path
    home = os.path.expanduser("~")
    ap = os.path.abspath(path)
    return ap.replace(home, "~", 1) if ap.startswith(home) else ap


def _read_text_safe(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:48] or "topic"


def _parse_iso(value) -> _dt.datetime | None:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        try:
            return _dt.datetime.combine(_dt.date.fromisoformat(s[:10]),
                                        _dt.time(0, 0), _dt.timezone.utc)
        except ValueError:
            return None


# --------------------------------------------------------------------------- #
# Search lanes — the routing target set. Each lane is a CHEAP description of a   #
# search kind: which intent it serves, which store backs it, and the exact next  #
# command. Command templates use {q}/{home}/{scripts}/{notes}/{key}/{path}.      #
# --------------------------------------------------------------------------- #
LANES = [
    {
        "id": "memory-entry",
        "title": "Hot stored fact / preference",
        "store": "semantic_memories",   # backed by the per-entry memories index
        "fallback_store": "hot_memory",
        "intent": re.compile(
            r"\b(prefer|preference|setting|config(?:ured|uration)?|default|rule|"
            r"policy|always|never|safety|api key|credential|secret|do i|my\b|"
            r"owner|user|fact|remember(?:ed)?|stored|pin|pinned)\b", re.I),
        "cost": "medium",
        "command": 'python3 {scripts}/memory_entry_index.py search "{q}" --home {home} --json',
        "cheap_alt": 'python3 {scripts}/memory_project.py --home {home} --query "{q}" --json',
        "why": "Concept search over MEMORY.md / USER.md entries (semantic if daemon up; else projection relevance).",
    },
    {
        "id": "session-semantic",
        "title": "Past conversation / how did we do X",
        "store": "semantic_sessions",
        "fallback_store": None,
        "intent": re.compile(
            r"\b(conversation|chat|session|how did we|did we (?:discuss|talk|decide)|"
            r"last time|earlier|previously discussed|we talked|remember when|"
            r"what did .* say|recall|transcript|past work|figure[d]? out)\b", re.I),
        "cost": "expensive",
        "command": 'python3 {scripts}/semantic_query.py "{q}" --hybrid --n 8 --json',
        "cheap_alt": "session_search tool (hybrid RRF; talks to the warm daemon from the agent venv)",
        "why": "Hybrid semantic + FTS retrieval over indexed session summaries.",
    },
    {
        "id": "temporal",
        "title": "What changed / history / since when",
        "store": "temporal",
        "fallback_store": None,
        "intent": re.compile(
            r"\b(chang(?:e|ed|es)|history|when did|since when|version|evolv|"
            r"used to|previously|prior|rollback|roll back|revert|diff|over time|"
            r"timeline|superseded|stale|outdated|update[d]?)\b", re.I),
        "cost": "cheap",
        "command": 'python3 {scripts}/temporal_memory.py current --home {home} --json',
        "cheap_alt": 'python3 {scripts}/temporal_memory.py history --key {key} --home {home} --json',
        "why": "Bi-temporal version history (current / point-in-time / diff) for a fact.",
    },
    {
        "id": "notes-canonical",
        "title": "Canonical docs / project status / spec",
        "store": "notes_index",
        "fallback_store": "master_index",
        "intent": re.compile(
            r"\b(status|project|spec|roadmap|plan|planning|canonical|note[s]?|"
            r"document(?:ation|ed)?|index|where is|where does|source of truth|"
            r"context|architecture|design|handoff|inventory)\b", re.I),
        "cost": "cheap",
        "command": 'read_file {path}',
        "cheap_alt": 'grep -rni "{q}" {notes}',
        "why": "Human-maintained long-form knowledge base; INDEX.md is the table of contents.",
    },
    {
        "id": "source-code",
        "title": "Package / source code implementation",
        "store": None,
        "fallback_store": None,
        "intent": re.compile(
            r"\b(code|implement(?:ation|ed)?|function|method|class|module|script|"
            r"bug|error|traceback|import|def |test[s]?|refactor|cli|argument|flag|"
            r"\.py\b|\.sh\b|installer|install\.sh)\b", re.I),
        "cost": "cheap",
        "command": 'grep -rni "{q}" {scripts}',
        "cheap_alt": "search_files / read_file over the package source tree",
        "why": "Direct source lookup; cheapest when the answer is in code, not memory.",
    },
    {
        "id": "spine",
        "title": "Evidence ledger / provenance (optional)",
        "store": "spine",
        "fallback_store": None,
        "intent": re.compile(
            r"\b(evidence|provenance|artifact|ledger|spine|authority|verif(?:y|ied|ication)|"
            r"who said|source for|citation|where did .* come from)\b", re.I),
        "cost": "medium",
        "command": 'sqlite3 -readonly {path} "SELECT * FROM event_fts WHERE event_fts MATCH \'{q}\' LIMIT 10"',
        "cheap_alt": "Memory Spine FTS (read-only); present only when the spine DB exists.",
        "why": "Indexed evidence/artifacts with authority + verification schema. Not required.",
    },
]

# Map a lane's backing store key to the health dict it should read.
_STORE_OF = {
    "semantic_memories": ("semantic", "memories"),
    "semantic_sessions": ("semantic", "sessions"),
    "temporal": ("temporal", "current_facts"),
    "notes_index": ("notes_index", "topics_indexed"),
    "master_index": ("master_index", "topics_indexed"),
    "hot_memory": ("hot_memory", "entries"),
    "spine": ("spine", "events"),
}


def _store_availability(map_obj: dict, store_key: str | None) -> tuple[str, int | None]:
    """('ok'|'stale'|'degraded'|'missing'|'n/a', count) for a lane's backing store."""
    if not store_key:
        return "n/a", None
    hk, ck = _STORE_OF.get(store_key, (store_key, None))
    h = (map_obj.get("stores") or {}).get(hk)
    if not h:
        return "missing", None
    count = h.get(ck) if ck else None
    status = h.get("status")
    if hk == "semantic":
        if h.get("daemon") == "up" and status == "ok":
            return "ok", count
        return "degraded", count
    if not h.get("present"):
        return "missing", count
    if status != "ok":
        return "degraded", count
    if h.get("fresh") is False:
        return "stale", count
    return "ok", count


# --------------------------------------------------------------------------- #
# build                                                                        #
# --------------------------------------------------------------------------- #
def build_map(home: str, *, today: _dt.date | None = None,
              scripts_dir: str | None = None, notes_dir: str | None = None,
              reports_dirs: list[str] | None = None,
              ping_timeout: float = 1.5,
              fresh_days: dict | None = None,
              max_topics: int = DEFAULT_MAX_TOPICS,
              semantic_health: dict | None = None) -> dict:
    """Assemble the cheap dynamic search map. Pure read-only; deterministic given
    (files, DBs, daemon counts, today).

    ``semantic_health`` is injectable for hermetic tests (the same shape
    probe_semantic returns); without it the live daemon is pinged over the socket.
    """
    home = resolve_home(home)
    today = today or _dt.date.today()
    fresh_days = {**DEFAULT_FRESH_DAYS, **(fresh_days or {})}
    scripts = resolve_scripts_dir(home, scripts_dir)
    nd = notes_dir or os.path.join(home, "notes")

    mem_h, usr_h, hot_topics = probe_hot_memory(home, today, fresh_days)
    notes_index_h, master_h, note_topics = probe_notes(home, today, fresh_days, nd)
    semantic_h = semantic_health if semantic_health is not None else probe_semantic(home, ping_timeout)
    temporal_h = probe_temporal(home, today, fresh_days)
    spine_h = probe_spine(home, today, fresh_days)
    shadow_h = probe_shadow_report(home, today, fresh_days, reports_dirs)

    stores = {
        "hot_memory": mem_h,
        "user_memory": usr_h,
        "notes_index": notes_index_h,
        "master_index": master_h,
        "semantic": semantic_h,
        "temporal": temporal_h,
        "spine": spine_h,
        "shadow_report": shadow_h,
    }

    # full topic pool kept on the map for query-time matching; the EMITTED slice
    # is salience-ranked and capped to keep markdown within budget.
    all_topics = hot_topics + note_topics
    ranked = sorted(all_topics, key=lambda t: (-t["_salience"], t["key"]))
    emitted = []
    seen = set()
    for t in ranked:
        sig = (t["lane"], t["key"])
        if sig in seen:
            continue
        seen.add(sig)
        emitted.append({
            "label": t["label"],
            "key": t["key"],
            "lane": t["lane"],
            "source": t["source"],
            "where": t["where"],
            "terms": t["terms"][:MAX_TERMS_PER_TOPIC],
        })
        if len(emitted) >= max_topics:
            break

    map_obj = {
        "tool": "memory_search_map",
        "tool_version": TOOL_VERSION,
        "generated_at": today.isoformat(),
        "home": _tilde(home),
        "scripts_dir": _tilde(scripts),
        "notes_dir": _tilde(nd),
        "budget_tokens": DEFAULT_MAP_TOKEN_BUDGET,
        "stores": stores,
        "topics": emitted,
        "topics_total": len(all_topics),
        "_all_topics": all_topics,  # internal; stripped before emission
    }

    # attach lane summaries (availability + the parameterless command form)
    map_obj["lanes"] = _lane_summaries(map_obj, home, scripts, nd)
    return map_obj


def _lane_path(lane_id: str, map_obj: dict, rep: dict | None) -> str:
    """Resolve the {path} placeholder for a lane's command."""
    if lane_id == "notes-canonical" and rep:
        return rep["where"]
    if lane_id == "spine":
        return (map_obj.get("stores", {}).get("spine", {}) or {}).get("path") or "<spine-db>"
    return "<canonical-path>"


def _lane_summaries(map_obj: dict, home: str, scripts: str, notes: str) -> list[dict]:
    out = []
    for lane in LANES:
        store_key = lane["store"]
        avail, count = _store_availability(map_obj, store_key)
        if avail in ("missing", "degraded") and lane.get("fallback_store"):
            favail, fcount = _store_availability(map_obj, lane["fallback_store"])
        else:
            favail, fcount = None, None
        path = _lane_path(lane["id"], map_obj, None)
        out.append({
            "id": lane["id"],
            "title": lane["title"],
            "cost": lane["cost"],
            "store": store_key,
            "availability": avail,
            "count": count,
            "fallback_store": lane.get("fallback_store"),
            "fallback_availability": favail,
            "why": lane["why"],
            "command": _fmt(lane["command"], home=home, scripts=scripts, notes=notes, path=path),
            "cheap_alt": _fmt(lane["cheap_alt"], home=home, scripts=scripts, notes=notes, path=path),
        })
    return out


def _fmt(template: str, *, q: str = "<query>", home: str = "~/.hermes",
         scripts: str = "scripts", notes: str = "~/.hermes/notes",
         key: str = "<fact-key>", path: str = "<canonical-path>") -> str:
    return template.format(q=q, home=_tilde(home) or home, scripts=_tilde(scripts) or scripts,
                           notes=_tilde(notes) or notes, key=key, path=path)


def _strip_internal(map_obj: dict) -> dict:
    """Return a JSON-safe copy without internal-only keys."""
    out = {k: v for k, v in map_obj.items() if not k.startswith("_")}
    return out


# --------------------------------------------------------------------------- #
# query — rank lanes by intent heuristics + lexical topic relevance            #
# --------------------------------------------------------------------------- #
# Scoring weights: intent regex hit, topic-term overlap, store availability.
W_INTENT = 2.0
W_TOPIC = 3.0
W_AVAIL = 1.0
_AVAIL_SCORE = {"ok": 1.0, "stale": 0.4, "degraded": 0.2, "n/a": 0.5, "missing": 0.0}


def rank_lanes(map_obj: dict, query: str, *, home: str | None = None,
               scripts_dir: str | None = None, notes_dir: str | None = None,
               top_topics: int = 3) -> dict:
    home = resolve_home(home or map_obj.get("home"))
    scripts = resolve_scripts_dir(home, scripts_dir)
    nd = notes_dir or os.path.join(home, "notes")
    qterms = set(query_terms(query))
    all_topics = map_obj.get("_all_topics") or []

    # lexical topic matches (relevance lane), grouped by lane and deduped by
    # (key, where) so the same fact surfacing from two sources isn't shown twice.
    matches_by_lane: dict[str, dict[tuple, dict]] = {}
    for t in all_topics:
        tset = set(t.get("terms") or [])
        overlap = qterms & tset
        if not overlap:
            continue
        # Jaccard-ish: reward shared terms, normalise by query size so a short
        # query that fully matches a topic scores high.
        score = len(overlap) / max(1, len(qterms))
        rec = {"label": t["label"], "key": t["key"], "where": t["where"],
               "lane": t["lane"], "source": t["source"],
               "matched_terms": sorted(overlap), "match_score": round(score, 3)}
        bucket = matches_by_lane.setdefault(t["lane"], {})
        sig = (t["key"], t["where"])
        if sig not in bucket or score > bucket[sig]["match_score"]:
            bucket[sig] = rec
    matches_by_lane = {
        lane_id: sorted(bucket.values(), key=lambda r: (-r["match_score"], r["key"]))
        for lane_id, bucket in matches_by_lane.items()
    }

    ranked = []
    for lane in LANES:
        intent_hits = len(lane["intent"].findall(query or ""))
        intent_score = 1.0 if intent_hits else 0.0
        lmatches = matches_by_lane.get(lane["id"], [])
        topic_score = max((m["match_score"] for m in lmatches), default=0.0)
        avail, count = _store_availability(map_obj, lane["store"])
        avail_score = _AVAIL_SCORE.get(avail, 0.0)
        total = (W_INTENT * intent_score + W_TOPIC * topic_score + W_AVAIL * avail_score)

        # pick a representative matched topic to fill {key}/{path}
        rep = lmatches[0] if lmatches else None
        key = rep["key"] if rep else "<fact-key>"
        path = _lane_path(lane["id"], map_obj, rep)
        cmd = _fmt(lane["command"], q=query, home=home, scripts=scripts, notes=nd, key=key, path=path)
        alt = _fmt(lane["cheap_alt"], q=query, home=home, scripts=scripts, notes=nd, key=key, path=path)

        reasons = []
        if intent_hits:
            reasons.append(f"intent match ({intent_hits})")
        if topic_score > 0:
            reasons.append(f"{len(lmatches)} topic hit(s)")
        reasons.append(f"store {avail}")

        ranked.append({
            "id": lane["id"],
            "title": lane["title"],
            "cost": lane["cost"],
            "score": round(total, 3),
            "store": lane["store"],
            "availability": avail,
            "store_count": count,
            "command": cmd,
            "cheap_alt": alt,
            "why": lane["why"],
            "reasons": reasons,
            "matched_topics": lmatches[:top_topics],
        })

    ranked.sort(key=lambda r: (-r["score"], LANE_ORDER.get(r["id"], 99)))
    # If nothing matched at all, surface a sane default ordering note.
    any_signal = any(r["score"] > W_AVAIL * _AVAIL_SCORE.get(r["availability"], 0.0) + 1e-9 for r in ranked)
    return {
        "tool": "memory_search_map",
        "tool_version": TOOL_VERSION,
        "mode": "query",
        "generated_at": map_obj.get("generated_at"),
        "home": _tilde(home),
        "query": query,
        "query_terms": sorted(qterms),
        "matched": any_signal,
        "recommended_lane": ranked[0]["id"] if ranked else None,
        "lanes": ranked,
        "note": None if any_signal else
            "No strong intent/topic signal; lanes ranked by store availability only. "
            "Start with the top lane or refine the query.",
    }


LANE_ORDER = {lane["id"]: i for i, lane in enumerate(LANES)}


# --------------------------------------------------------------------------- #
# doctor — freshness/health verdict                                            #
# --------------------------------------------------------------------------- #
def doctor(map_obj: dict, *, fresh_days: dict | None = None, strict: bool = False) -> dict:
    fresh_days = {**DEFAULT_FRESH_DAYS, **(fresh_days or {})}
    stores = map_obj.get("stores") or {}
    failures: list[str] = []
    warnings: list[str] = []
    oks: list[str] = []

    # Hot memory (MEMORY.md) is the ONE required store.
    mem = stores.get("hot_memory") or {}
    if not mem.get("present"):
        failures.append("hot_memory (MEMORY.md) is MISSING — the stack has no hot tier to route to")
    elif mem.get("status") != "ok":
        failures.append(f"hot_memory unreadable ({mem.get('status')})")
    else:
        oks.append(f"hot_memory: {mem.get('entries')} entries, {mem.get('capacity_pct')}% capacity")
        if mem.get("fresh") is False:
            warnings.append(f"hot_memory stale (mtime {mem.get('age_days')}d > {fresh_days['hot_memory']}d)")

    # Optional stores → WARN when missing/stale, never FAIL.
    def _opt(key: str, label: str, fkey: str):
        h = stores.get(key) or {}
        if not h.get("present"):
            warnings.append(f"{label} missing ({h.get('path')})")
            return
        if h.get("status") not in ("ok",):
            warnings.append(f"{label} degraded ({h.get('status')})")
            return
        if h.get("fresh") is False:
            warnings.append(f"{label} stale (age {h.get('age_days')}d > {fresh_days.get(fkey)}d)")
        else:
            oks.append(f"{label} ok")

    _opt("user_memory", "user_memory (USER.md)", "user_memory")
    _opt("notes_index", "notes_index (INDEX.md)", "notes_index")
    _opt("master_index", "master_index (MASTER_CONTEXT_INDEX.md)", "master_index")
    _opt("temporal", "temporal (memory_versions.db)", "temporal")
    _opt("shadow_report", "shadow_report", "shadow_report")

    # Semantic daemon: WARN when down (retrieval falls back to subprocess/static).
    sem = stores.get("semantic") or {}
    if sem.get("daemon") == "up":
        oks.append(f"semantic daemon up (sessions={sem.get('sessions')}, memories={sem.get('memories')})")
    else:
        warnings.append(f"semantic daemon {sem.get('status')} — semantic lanes degrade to subprocess/static")

    # Spine is genuinely optional; only note when present-and-readable.
    spine = stores.get("spine") or {}
    if spine.get("present") and spine.get("status") == "ok":
        oks.append(f"spine ok (events={spine.get('events')}, artifacts={spine.get('artifacts')})")
    elif spine.get("present"):
        warnings.append(f"spine present but {spine.get('status')}")

    status = "FAIL" if failures else ("WARN" if warnings else "PASS")
    return {
        "tool": "memory_search_map",
        "tool_version": TOOL_VERSION,
        "mode": "doctor",
        "generated_at": map_obj.get("generated_at"),
        "home": map_obj.get("home"),
        "status": status,
        "failures": failures,
        "warnings": warnings,
        "ok": oks,
        "store_summary": {k: (v or {}).get("status") for k, v in stores.items()},
    }


# --------------------------------------------------------------------------- #
# Markdown rendering (compact, <= budget tokens)                               #
# --------------------------------------------------------------------------- #
def render_map_markdown(map_obj: dict) -> str:
    s = map_obj["stores"]
    sem = s.get("semantic", {})
    lines = [
        f"# Memory Search Map ({map_obj['generated_at']})",
        "",
        "First-pass router: pick a lane below, then run its command. Cheap (<=1000 tok), read-only.",
        "",
        "## Stores",
        "",
        "| Store | State | Count | Fresh |",
        "|---|---|---:|:--:|",
    ]
    rows = [
        ("hot_memory", s.get("hot_memory", {}), "entries"),
        ("user_memory", s.get("user_memory", {}), "entries"),
        ("notes_index", s.get("notes_index", {}), "topics_indexed"),
        ("master_index", s.get("master_index", {}), "topics_indexed"),
        ("temporal", s.get("temporal", {}), "current_facts"),
        ("spine", s.get("spine", {}), "events"),
    ]
    for name, h, ck in rows:
        fresh = {True: "✓", False: "stale", None: "—"}[h.get("fresh")]
        lines.append(f"| {name} | {h.get('status','?')} | {h.get(ck) if h.get(ck) is not None else '—'} | {fresh} |")
    lines.append(f"| semantic | {sem.get('daemon','?')} | s={sem.get('sessions')}/m={sem.get('memories')} | — |")
    sh = s.get("shadow_report", {})
    lines.append(f"| shadow_report | {sh.get('report_status') or sh.get('status')} | — | "
                 f"{ {True:'✓', False:'stale', None:'—'}[sh.get('fresh')] } |")

    lines += ["", "## Lanes", "", "| Lane | When | Store | Command |", "|---|---|---|---|"]
    for lane in map_obj.get("lanes", []):
        avail = lane["availability"]
        store_cell = f"{lane['store'] or 'code'} ({avail})"
        lines.append(f"| `{lane['id']}` | {lane['title']} | {store_cell} | `{lane['command']}` |")

    lines += ["", "## Top topics", ""]
    if map_obj.get("topics"):
        for t in map_obj["topics"][:14]:
            lines.append(f"- **{t['key']}** ({t['lane']}) — {t['label']} → `{t['where']}`")
    else:
        lines.append("- (none indexed)")
    lines.append("")
    return "\n".join(lines)


def _trim_markdown_to_budget(map_obj: dict, md: str, budget: int) -> tuple[str, bool]:
    """If the markdown exceeds the token budget, drop topics from the tail until it
    fits. Returns (markdown, trimmed)."""
    if est_tokens(md) <= budget:
        return md, False
    topics = list(map_obj.get("topics", []))
    trimmed = False
    while topics and est_tokens(md) > budget:
        topics = topics[:-1]
        trimmed = True
        clone = dict(map_obj)
        clone["topics"] = topics
        md = render_map_markdown(clone)
    if est_tokens(md) > budget:
        md = md.rstrip() + "\n\n_(truncated to fit token budget)_\n"
    return md, trimmed


def render_query_markdown(result: dict) -> str:
    lines = [
        f"# Search route for: {redact(result['query'])!r}",
        "",
        f"Recommended lane: **{result.get('recommended_lane')}**"
        + ("" if result.get("matched") else "  _(no strong signal — availability-ranked)_"),
        "",
    ]
    for i, lane in enumerate(result["lanes"], 1):
        if lane["score"] <= 0 and i > 3:
            continue
        lines.append(f"## {i}. `{lane['id']}` — score {lane['score']} ({', '.join(lane['reasons'])})")
        lines.append(f"- {lane['title']} · cost: {lane['cost']} · store: {lane['store'] or 'code'} ({lane['availability']})")
        lines.append(f"- Run: `{lane['command']}`")
        if lane.get("cheap_alt"):
            lines.append(f"- Alt: `{lane['cheap_alt']}`")
        for m in lane.get("matched_topics", []):
            lines.append(f"  - topic `{m['key']}` ({', '.join(m['matched_terms'])}) → `{m['where']}`")
        lines.append("")
    return "\n".join(lines)


def render_doctor_markdown(report: dict) -> str:
    lines = [f"# Search Map Doctor — {report['status']}", ""]
    if report["failures"]:
        lines += ["## Failures", ""] + [f"- {x}" for x in report["failures"]] + [""]
    if report["warnings"]:
        lines += ["## Warnings", ""] + [f"- {x}" for x in report["warnings"]] + [""]
    lines += ["## OK", ""] + [f"- {x}" for x in report["ok"]] + [""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", help="Hermes home (default $HERMES_HOME or ~/.hermes)")
    common.add_argument("--scripts-dir", help="dir holding the runnable scripts for emitted commands "
                        "(default <home>/scripts, else this package's scripts/)")
    common.add_argument("--notes-dir", help="notes dir (default <home>/notes)")
    common.add_argument("--reports-dir", action="append", dest="reports_dirs",
                        help="shadow-report dir to scan (repeatable; sensible defaults otherwise)")
    common.add_argument("--ping-timeout", type=float, default=1.5,
                        help="semantic daemon ping timeout in seconds (default 1.5)")
    common.add_argument("--today", help="override today (YYYY-MM-DD) for deterministic runs")
    common.add_argument("--max-tokens", type=int, default=DEFAULT_MAP_TOKEN_BUDGET,
                        help=f"markdown token budget (default {DEFAULT_MAP_TOKEN_BUDGET})")
    common.add_argument("--markdown", action="store_true", help="emit compact markdown")
    common.add_argument("--json", action="store_true", help="emit JSON (default)")
    common.add_argument("--out", help="also write the output to this file")

    p = argparse.ArgumentParser(
        prog="memory_search_map.py",
        description="Cheap dynamic search map: route to the right search lane before "
                    "spending on retrieval. READ-ONLY; stdlib only; no LLM/embeddings.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("build", parents=[common], help="emit the compact map from current stores")
    q = sub.add_parser("query", parents=[common], help="rank lanes + emit exact next commands for --query")
    q.add_argument("--query", required=True, help="the thing the agent is missing / looking for")
    q.add_argument("--top-topics", type=int, default=3, help="matched topics to show per lane (default 3)")
    d = sub.add_parser("doctor", parents=[common], help="validate store freshness/health")
    d.add_argument("--strict", action="store_true", help="exit 1 on WARN as well as FAIL")
    p.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    return p


def _parse_today(s: str | None) -> _dt.date | None:
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(s)
    except ValueError:
        raise SystemExit(f"error: --today must be YYYY-MM-DD, got {s!r}")


def _emit(text: str, out: str | None) -> None:
    if out:
        op = os.path.abspath(os.path.expanduser(out))
        os.makedirs(os.path.dirname(op) or ".", exist_ok=True)
        with open(op, "w", encoding="utf-8") as fh:
            fh.write(text + ("\n" if not text.endswith("\n") else ""))
    print(text)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    home = resolve_home(args.home)
    today = _parse_today(args.today)
    map_obj = build_map(
        home, today=today, scripts_dir=args.scripts_dir, notes_dir=args.notes_dir,
        reports_dirs=args.reports_dirs, ping_timeout=args.ping_timeout)

    want_md = args.markdown and not args.json

    if args.cmd == "build":
        if want_md:
            md = render_map_markdown(_public_map(map_obj))
            md, _ = _trim_markdown_to_budget(map_obj, md, args.max_tokens)
            _emit(md, args.out)
        else:
            _emit(json.dumps(_strip_internal(map_obj), indent=2, ensure_ascii=False), args.out)
        return 0

    if args.cmd == "query":
        result = rank_lanes(map_obj, args.query, home=home,
                            scripts_dir=args.scripts_dir, notes_dir=args.notes_dir,
                            top_topics=args.top_topics)
        if want_md:
            _emit(render_query_markdown(result), args.out)
        else:
            _emit(json.dumps(result, indent=2, ensure_ascii=False), args.out)
        return 0

    if args.cmd == "doctor":
        report = doctor(map_obj, strict=args.strict)
        if want_md:
            _emit(render_doctor_markdown(report), args.out)
        else:
            _emit(json.dumps(report, indent=2, ensure_ascii=False), args.out)
        if report["status"] == "FAIL" or (args.strict and report["status"] == "WARN"):
            return 1
        return 0

    return 2  # pragma: no cover


def _public_map(map_obj: dict) -> dict:
    return _strip_internal(map_obj)


if __name__ == "__main__":
    raise SystemExit(main())
