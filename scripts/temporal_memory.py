#!/usr/bin/env python3
"""Temporal versioning for Hermes hot-memory (MEMORY.md / USER.md).

Adds a *time axis* to the flat §-delimited memory files without touching their
wire format, state.db, config.yaml, or the running gateway. Design doc:
~/.hermes/plans/temporal-memory-versioning-design.md

Architecture (A+B hybrid):
  * SOURCE OF TRUTH  : append-only JSONL event log
                       ~/.hermes/memories/_versions/history.jsonl
  * QUERY INDEX      : derived, rebuildable SQLite ~/.hermes/memory_versions.db
                       (pure projection; `--rebuild` replays the JSONL)

Bi-temporal model:
  * TRANSACTION time (recorded_at + a monotonic per-event `seq`) is the
    AUTHORITATIVE order. Version numbers, the supersession chain, and which
    version is `current` are all derived from transaction order. This is the
    natural event-sourcing model and is fully deterministic (seq breaks ties).
  * VALID time (real-world) is a separate interval per version. The effective
    valid_from is COALESCE(valid_from, recorded_at) — a fact cannot be true
    before Hermes recorded it, absent an explicit backdate. A version's
    valid_to is closed at its successor's effective valid_from (contiguous, no
    gaps); the last version stays open unless it carries an explicit valid_to
    (e.g. an archived-and-retired fact with no live successor).

The materializer is the single source of bi-temporal truth and is identical
for incremental writes and full rebuilds — no drift is possible.

stdlib only. Self-contained (best-effort house imports, clean fallback).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

try:  # advisory locking (POSIX); degrade gracefully if unavailable
    import fcntl  # type: ignore
except Exception:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
# --------------------------------------------------------------------------- #
# Format constants — the on-disk hot-memory shape. ENTRY_DELIMITER /           #
# POINTER_SIGIL / HEADER_SENTINEL are imported from memory_signals (INTEG-8),  #
# the single source of truth shared with the curator / audit / intake gate.    #
# --------------------------------------------------------------------------- #
from memory_signals import ENTRY_DELIMITER, POINTER_SIGIL, HEADER_SENTINEL  # noqa: E402
PER_ENTRY_LIMIT = 2000               # generous guard for restore --apply validation
SCHEMA_REV = "2"

_STOPLIST = {
    "the", "and", "for", "with", "that", "this", "from", "into", "via", "are",
    "was", "now", "not", "but", "you", "use", "uses", "used", "per", "out",
    "all", "any", "has", "have", "his", "her", "its", "new", "old",
    "user", "emeka", "hermes", "note", "notes", "should", "must", "can",
}
_GENERIC_KEYS = {
    "user", "user-preference", "user-pref", "note", "notes", "update",
    "updates", "todo", "wip", "memory", "entry", "fact", "context",
}

_TOPIC_RE = re.compile(r"^([A-Za-z][\w &/+.\-]{1,46}?)\s*(?:\([^)]*\))?\s*:")
_DATE_RE = re.compile(r"(?<![\w/])(\d{4})-(\d{2})-(\d{2})(?![\w/])")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
# An archived-pointer's embedded archive file reference, e.g.
#   "→ archived 2026-06-23. Find: ... or ~/.hermes/memories/_archive/curator/2026-06-23-MEMORY.md"
_PTR_ARCHIVE_RE = re.compile(r"archived\s+(\d{4}-\d{2}-\d{2})\b")
_PTR_ARCHIVE_FILE_RE = re.compile(r"_archive/curator/([\w.\-]+-MEMORY\.md)")


# --------------------------------------------------------------------------- #
# Pure helpers                                                                #
# --------------------------------------------------------------------------- #
def content_hash(text: str) -> str:
    """16-hex content fingerprint — identical to curator content_hash()."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def slugify(text: str) -> str:
    """First line -> lowercase -> non-alnum to '-' -> trim -> [:48]. Matches curator."""
    first = text.strip().splitlines()[0] if text.strip() else "entry"
    s = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-")
    return (s[:48] or "entry")


def first_line(text: str) -> str:
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def meaningful_tokens(text: str) -> list[str]:
    return [t for t in _WORD_RE.findall(text.lower()) if t not in _STOPLIST]


def pointer_topic(text: str) -> str:
    """For a '↪ <topic>… → archived ...' pointer, return the bare <topic>."""
    body = text.strip()
    if body.startswith(POINTER_SIGIL):
        body = body[len(POINTER_SIGIL):].strip()
    body = re.split(r"\s*→\s*archived\b", body, maxsplit=1)[0]
    return body.rstrip(" .…").strip()


def is_pointer(text: str) -> bool:
    return text.strip().startswith(POINTER_SIGIL)


def derive_key(text: str) -> str:
    """Stable logical-fact identity for an entry (see design §3.2)."""
    if text.strip().startswith(HEADER_SENTINEL):
        return "notes-system-header"
    base = pointer_topic(text) if is_pointer(text) else first_line(text)
    m = _TOPIC_RE.match(base)
    if m:
        topic_slug = slugify(m.group(1))
        if topic_slug not in _GENERIC_KEYS:
            return topic_slug
    toks = meaningful_tokens(base)[:6]
    return slugify(" ".join(toks)) if toks else slugify(base)


def jaccard(a: str, b: str) -> float:
    sa, sb = set(meaningful_tokens(a)), set(meaningful_tokens(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def latest_date(text: str) -> str | None:
    """Latest non-path YYYY-MM-DD in the text as an ISO datetime (UTC midnight)."""
    dates = []
    for y, mo, d in _DATE_RE.findall(text):
        try:
            dates.append(_dt.date(int(y), int(mo), int(d)))
        except ValueError:
            continue
    if not dates:
        return None
    return _dt.datetime.combine(max(dates), _dt.time(0, 0), _dt.timezone.utc).isoformat()


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _split_jsonl(text: str) -> list[str]:
    r"""Split a JSONL blob into records on '\n' ONLY — never str.splitlines().
    splitlines() also breaks on U+2028 / U+2029 / U+0085, which json.dumps(
    ensure_ascii=False) emits RAW inside an event's content. Splitting on those
    would shred a single valid record into fragments that the resilient reader
    then quarantines and DROPS — silent loss of any event containing such a
    character (common in PDF/Word/web/JS-origin text). (adversarial review: CRITICAL)"""
    return text.split("\n")


def parse_dt(value) -> str | None:
    """Robustly parse a date/datetime/epoch into an ISO-8601 UTC string."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if re.fullmatch(r"\d{9,11}", value):
        return _dt.datetime.fromtimestamp(int(value), _dt.timezone.utc).replace(microsecond=0).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        d = _dt.date.fromisoformat(value)
        return _dt.datetime.combine(d, _dt.time(0, 0), _dt.timezone.utc).isoformat()
    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat()
    except ValueError:
        raise SystemExit(f"unparseable date/time: {value!r} (use YYYY-MM-DD or ISO-8601)")


def _txn_key(ev: dict) -> tuple:
    """Authoritative transaction-time ordering of a fact's versions.
    Deterministic: `seq` (monotonic append counter) is the final, unique tiebreak."""
    return (ev.get("recorded_at") or "", int(ev.get("seq") or 0), ev.get("event_id", ""))


def _eff_valid_from(ev: dict) -> str | None:
    """Effective real-world start: explicit valid_from, else the recorded_at floor."""
    return ev.get("valid_from") or ev.get("recorded_at")


# --------------------------------------------------------------------------- #
# Home / DB plumbing (best-effort house imports, clean stdlib fallback)       #
# --------------------------------------------------------------------------- #
def _resolve_home(explicit: str | None) -> Path:
    """Resolve HERMES_HOME with NO hardcoded author path (EXPORT-5 / INTEG-11):
      1. explicit --home argument
      2. $HERMES_HOME
      3. ~/.hermes  (Path.home() fallback)
    The optional house import (hermes_constants) is probed ONLY via a path
    derived RELATIVE to this script (…/hermes-agent), never an absolute author
    home, so a second user who sets neither --home nor $HERMES_HOME can never be
    silently routed into another user's memory store."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    cand = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hermes-agent"))
    if Path(cand).is_dir():
        # Consult hermes_constants ONLY when the script-relative dir is present
        # (keeps the probe scoped exactly as the docstring promises — never an
        # ambient/absolute author home).
        if cand not in sys.path:
            sys.path.insert(0, cand)
        try:
            from hermes_constants import get_hermes_home  # type: ignore
            return Path(get_hermes_home())
        except Exception:
            pass
    return Path.home() / ".hermes"


def _apply_wal(conn: sqlite3.Connection, label: str) -> None:
    try:
        from hermes_state import apply_wal_with_fallback  # type: ignore
        apply_wal_with_fallback(conn, db_label=label)
        return
    except Exception:
        pass
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass


def _lockpath(p: Path) -> Path:
    """Lock file path matching the curator/memory-tool convention: <file>.lock
    (e.g. MEMORY.md -> MEMORY.md.lock, history.jsonl -> history.jsonl.lock)."""
    return Path(str(p) + ".lock")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
  event_id      TEXT PRIMARY KEY,
  fact_key      TEXT NOT NULL,
  store         TEXT NOT NULL,
  version       INTEGER NOT NULL,
  seq           INTEGER,
  op            TEXT NOT NULL,
  title         TEXT,
  content       TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  taxonomy      TEXT,
  valid_from    TEXT,
  eff_valid_from TEXT,
  valid_to      TEXT,
  recorded_at   TEXT NOT NULL,
  superseded_at TEXT,
  supersedes    TEXT,
  superseded_by TEXT,
  source        TEXT,
  confidence    REAL,
  actor         TEXT,
  reason        TEXT,
  tags          TEXT,
  archived_path TEXT,
  is_current    INTEGER NOT NULL DEFAULT 0,
  tombstone     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ver_key       ON versions(fact_key, version);
CREATE INDEX IF NOT EXISTS idx_ver_current   ON versions(store, is_current);
CREATE INDEX IF NOT EXISTS idx_ver_hash      ON versions(content_hash);
CREATE INDEX IF NOT EXISTS idx_ver_recorded  ON versions(recorded_at);
CREATE INDEX IF NOT EXISTS idx_ver_efffrom   ON versions(eff_valid_from);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_FIELDS = [
    "event_id", "fact_key", "store", "version", "seq", "op", "title", "content",
    "content_hash", "taxonomy", "valid_from", "eff_valid_from", "valid_to",
    "recorded_at", "superseded_at", "supersedes", "superseded_by", "source",
    "confidence", "actor", "reason", "tags", "archived_path", "is_current", "tombstone",
]


class TemporalMemoryError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Core                                                                        #
# --------------------------------------------------------------------------- #
class TemporalMemory:
    def __init__(self, home: str | None = None, *, db_path: str | None = None,
                 jsonl_path: str | None = None):
        self.home = _resolve_home(home)
        self.memories_dir = self.home / "memories"
        self.versions_dir = self.memories_dir / "_versions"
        self.archive_dir = self.memories_dir / "_archive" / "curator"
        self.jsonl = Path(jsonl_path).expanduser() if jsonl_path else (self.versions_dir / "history.jsonl")
        self.cold_jsonl = self.jsonl.with_name("history.archive.jsonl")
        self.corrupt_jsonl = self.jsonl.with_name("history.jsonl.corrupt")
        self.db_path = Path(db_path).expanduser() if db_path else (self.home / "memory_versions.db")
        self._read_warnings: list[str] = []

        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        _apply_wal(self.conn, "memory_versions.db")
        self.conn.executescript(_SCHEMA)
        self._ensure_schema()      # heal a stale/older derived index (it is rebuildable)
        self.conn.commit()
        # Auto-rebuild on drift, but never let a bad log brick read commands.
        try:
            self._ensure_fresh()
        except Exception as e:  # pragma: no cover - defensive
            self._read_warnings.append(f"ensure_fresh skipped: {e}")

    # -- locking ---------------------------------------------------------- #
    @contextmanager
    def _lock(self, lock_path: Path):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    # -- JSONL source of truth ------------------------------------------- #
    def _read_events(self) -> list[dict]:
        """Resilient read: skip + quarantine corrupt lines, assign a stable `seq`
        from append order when missing, so a single bad line never bricks the tool."""
        if not self.jsonl.exists():
            return []
        out, bad = [], []
        for i, line in enumerate(_split_jsonl(self.jsonl.read_text(encoding="utf-8", errors="replace"))):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if not isinstance(ev, dict) or "fact_key" not in ev or "content" not in ev:
                    raise ValueError("missing required keys")
            except (json.JSONDecodeError, ValueError) as e:
                bad.append(line)
                self._read_warnings.append(f"line {i}: {e}")
                continue
            ev.setdefault("seq", i)            # back-compat: file order is the floor
            ev.setdefault("recorded_at", ev.get("valid_from") or now_iso())
            ev.setdefault("event_id", uuid.uuid4().hex)
            out.append(ev)
        if bad:
            # Idempotent quarantine: a persistent torn line is re-detected on EVERY
            # read, so dedup against what's already quarantined to bound the growth
            # of history.jsonl.corrupt. (adversarial review: MEDIUM)
            try:
                seen = set()
                if self.corrupt_jsonl.exists():
                    seen = set(_split_jsonl(
                        self.corrupt_jsonl.read_text(encoding="utf-8", errors="replace")))
                fresh = [b for b in bad if b not in seen]
                if fresh:
                    with open(self.corrupt_jsonl, "a", encoding="utf-8") as f:
                        for b in fresh:
                            f.write(b + "\n")
            except OSError:
                pass
        return out

    def _next_seq(self, events: list[dict]) -> int:
        return (max((int(e.get("seq") or 0) for e in events), default=-1)) + 1

    @staticmethod
    def _ensure_trailing_newline(path: Path) -> None:
        r"""Guarantee the append-only log ends in b'\n' so the NEXT append cannot
        glue onto a complete-but-unterminated last line and silently destroy the
        new event. MUST be called under the file's lock, immediately before an
        append. No-op on a missing or empty file. (SAFETY-2)"""
        try:
            with open(path, "r+b") as f:
                f.seek(0, os.SEEK_END)
                if f.tell() == 0:
                    return
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    f.write(b"\n")
                    f.flush()
                    os.fsync(f.fileno())
        except FileNotFoundError:
            return

    def _rewrite_hot_log(self, events: list[dict]) -> None:
        """Atomically rewrite history.jsonl to exactly `events` (one JSON object
        per line, newline-terminated). Used to HEAL a torn/corrupt last line:
        _read_events() quarantines such a line but cannot remove it from the hot
        log, so without this every later append keeps gluing onto the poison.
        Caller MUST hold the JSONL lock. (SAFETY-2)"""
        tmp = self.jsonl.with_name(self.jsonl.name + ".heal.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.jsonl)
        self._fsync_dir(self.jsonl.parent)

    # -- index freshness / rebuild --------------------------------------- #
    def _fingerprint(self) -> str:
        if not self.jsonl.exists():
            return "0:0"
        st = self.jsonl.stat()
        # size+mtime+linecount is cheap and catches in-place edits; full content
        # equality is not required because the log is append-only in normal use.
        n = sum(1 for ln in _split_jsonl(self.jsonl.read_text(encoding="utf-8", errors="replace")) if ln.strip())
        return f"{st.st_size}:{int(st.st_mtime)}:{n}"

    def _ensure_fresh(self) -> None:
        fp = self._fingerprint()
        row = self.conn.execute("SELECT value FROM meta WHERE key='jsonl_fp'").fetchone()
        if not row or row["value"] != fp:
            self.rebuild()

    def rebuild(self, *, force: bool = False) -> dict:
        """Replay the JSONL into a freshly materialized SQLite index.
        Refuses to silently wipe a populated index if the log vanished."""
        events = self._read_events()
        if not events:
            have_rows = self.conn.execute("SELECT COUNT(*) n FROM versions").fetchone()["n"]
            row = self.conn.execute("SELECT value FROM meta WHERE key='jsonl_events'").fetchone()
            claimed = int(row["value"]) if row else 0
            if (have_rows or claimed) and not force and not self.jsonl.exists():
                raise TemporalMemoryError(
                    f"history.jsonl missing but index has {have_rows} rows / meta claims {claimed}; "
                    f"refusing to wipe. Restore the log or call rebuild(force=True).")
        # de-duplicate by event_id (last-writer-wins) so a duplicated line never
        # crashes the PRIMARY KEY insert.
        dedup: dict[str, dict] = {}
        for ev in events:
            dedup[ev["event_id"]] = ev
        events = list(dedup.values())
        self.conn.execute("DELETE FROM versions")
        by_key: dict[str, list[dict]] = {}
        for ev in events:
            by_key.setdefault(ev["fact_key"], []).append(ev)
        for key, evs in by_key.items():
            self._materialize_into_db(key, evs)
        self._set_meta("jsonl_events", str(len(events)))
        self._set_meta("jsonl_fp", self._fingerprint())
        self._set_meta("schema_rev", SCHEMA_REV)
        self._set_meta("last_rebuild", now_iso())
        self.conn.commit()
        return {"events": len(events), "facts": len(by_key),
                "read_warnings": len(self._read_warnings)}

    def _ensure_schema(self) -> None:
        """The SQLite index is a derived projection. If it exists with a different
        column shape (older version, or a sibling tool created it), drop and
        recreate it; _ensure_fresh then replays the JSONL into the new schema."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(versions)").fetchall()}
        if cols and cols != set(_FIELDS):
            self.conn.execute("DROP TABLE versions")
            self.conn.executescript(_SCHEMA)
            self._set_meta("jsonl_fp", "")   # force a rebuild from the source of truth

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def _materialize_into_db(self, fact_key: str, events: list[dict]) -> None:
        """Compute authoritative version/interval/current fields for one fact and
        upsert all its rows. Ordering is TRANSACTION time (recorded_at, seq);
        valid intervals are contiguous on the effective-valid-from axis."""
        # de-dup within the fact by event_id (defensive) then order by transaction time
        seen: dict[str, dict] = {}
        for ev in events:
            seen[ev["event_id"]] = ev
        evs = sorted(seen.values(), key=_txn_key)
        n = len(evs)
        # current = the LAST version in transaction order, iff it is live:
        # not a delete tombstone and not explicitly closed (its own valid_to None).
        current_idx = None
        if n:
            last = evs[n - 1]
            if last.get("op") != "delete" and not last.get("valid_to"):
                current_idx = n - 1
        self.conn.execute("DELETE FROM versions WHERE fact_key=?", (fact_key,))
        for i, ev in enumerate(evs):
            nxt = evs[i + 1] if i + 1 < n else None
            eff_from = _eff_valid_from(ev)
            # contiguous close at the successor's effective valid_from (no gaps);
            # the last version keeps its own explicit valid_to (open-ended if None).
            if nxt is not None:
                # Clamp so a backdated successor (new version whose effective
                # valid_from precedes this one) yields a zero-length interval
                # rather than an INVERTED [newer, older) one that would erase
                # this version from valid-axis point-in-time queries.
                nxt_from = _eff_valid_from(nxt)
                valid_to = max(nxt_from, eff_from) if (nxt_from and eff_from) else nxt_from
            else:
                valid_to = ev.get("valid_to")
            tombstone = 1 if ev.get("op") == "delete" else 0
            is_current = 1 if (i == current_idx and tombstone == 0) else 0
            row = {
                "event_id": ev["event_id"], "fact_key": fact_key,
                "store": ev.get("store", "MEMORY.md"), "version": i + 1,
                "seq": int(ev.get("seq") or 0), "op": ev.get("op", "update"),
                "title": ev.get("title"), "content": ev.get("content", "") or "",
                "content_hash": ev.get("content_hash") or content_hash(ev.get("content", "") or ""),
                "taxonomy": ev.get("taxonomy"), "valid_from": ev.get("valid_from"),
                "eff_valid_from": eff_from, "valid_to": valid_to,
                "recorded_at": ev.get("recorded_at") or now_iso(),
                "superseded_at": (nxt.get("recorded_at") if nxt else None),
                "supersedes": (evs[i - 1]["event_id"] if i > 0 else None),
                "superseded_by": (nxt.get("event_id") if nxt else None),
                "source": ev.get("source"), "confidence": ev.get("confidence"),
                "actor": ev.get("actor"), "reason": ev.get("reason"),
                "tags": json.dumps(ev.get("tags") or [], ensure_ascii=False),
                "archived_path": ev.get("archived_path"),
                "is_current": is_current, "tombstone": tombstone,
            }
            self.conn.execute(
                f"INSERT INTO versions ({','.join(_FIELDS)}) "
                f"VALUES ({','.join('?' for _ in _FIELDS)})",
                [row[f] for f in _FIELDS])

    def _fact_events(self, fact_key: str) -> list[dict]:
        return [e for e in self._read_events() if e["fact_key"] == fact_key]

    def _current_hash(self, fact_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT content_hash FROM versions WHERE fact_key=? AND is_current=1", (fact_key,)).fetchone()
        return row["content_hash"] if row else None

    # -- write path ------------------------------------------------------- #
    def record(self, *, fact_key: str, content: str, store: str = "MEMORY.md",
               op: str = "update", source: str = "manual", title: str | None = None,
               taxonomy: str | None = None, valid_from: str | None = None,
               valid_to: str | None = None, confidence: float | None = None,
               actor: str | None = None, reason: str | None = None,
               tags: list[str] | None = None, archived_path: str | None = None,
               recorded_at: str | None = None, allow_duplicate: bool = False) -> dict | None:
        """Append one immutable version event and re-materialize the fact.
        The whole check-and-append is done under the JSONL lock so dedup is
        atomic across processes. Idempotent against the CURRENT version only:
        a legitimate reversion (A->B->A) is recorded, not swallowed."""
        chash = content_hash(content)
        with self._lock(_lockpath(self.jsonl)):
            pre_warn = len(self._read_warnings)
            events = self._read_events()
            corrupt_found = len(self._read_warnings) > pre_warn
            if not allow_duplicate:
                # dedup ONLY against what is currently live for this fact
                cur = [e for e in events if e["fact_key"] == fact_key]
                cur_sorted = sorted(cur, key=_txn_key)
                if cur_sorted and cur_sorted[-1].get("op") != "delete" \
                        and content_hash(cur_sorted[-1].get("content", "")) == chash:
                    # No-op write, but still heal a torn line out of the hot log so
                    # a duplicate-heavy workload can't leave it poisoned forever.
                    if corrupt_found:
                        self._rewrite_hot_log(events)
                    return None  # identical to current -> no-op
            ev = {
                "event_id": uuid.uuid4().hex, "fact_key": fact_key, "store": store,
                "seq": self._next_seq(events), "op": op,
                "title": title or first_line(content)[:120],
                "content": content, "content_hash": chash, "taxonomy": taxonomy,
                "valid_from": parse_dt(valid_from) if valid_from else latest_date(content),
                "valid_to": parse_dt(valid_to) if valid_to else None,
                "recorded_at": parse_dt(recorded_at) if recorded_at else now_iso(),
                "source": source, "confidence": confidence,
                "actor": actor or "temporal_memory.py", "reason": reason,
                "tags": tags or [], "archived_path": archived_path,
            }
            if corrupt_found:
                # A torn/corrupt line was quarantined on read; rewrite the hot
                # log to the good events + this new one so the poison cannot
                # re-glue on the next append (atomic tmp+fsync+os.replace).
                self._rewrite_hot_log(events + [ev])
            else:
                # Heal a missing trailing newline, then append durably so this
                # event can never glue onto an unterminated last line. (SAFETY-2)
                self._ensure_trailing_newline(self.jsonl)
                with open(self.jsonl, "a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
        # rematerialize this fact (cheap) and refresh meta/fingerprint
        self._materialize_into_db(fact_key, self._fact_events(fact_key))
        self._set_meta("jsonl_events", str(len(self._read_events())))
        self._set_meta("jsonl_fp", self._fingerprint())
        self._set_meta("last_write", now_iso())
        self.conn.commit()
        return ev

    # -- identity matching (shared with auto-extraction) ------------------ #
    def match(self, text: str, *, store: str | None = None, threshold: float = 0.6) -> dict:
        """Decide NEW / UPDATE / DUPLICATE for an incoming entry."""
        chash = content_hash(text)
        rows = self.conn.execute(
            "SELECT fact_key, content, content_hash FROM versions WHERE is_current=1"
            + (" AND store=?" if store else ""),
            ([store] if store else [])).fetchall()
        for r in rows:
            if r["content_hash"] == chash:
                return {"action": "DUPLICATE", "fact_key": r["fact_key"], "score": 1.0}
        key = derive_key(text)
        for r in rows:
            if r["fact_key"] == key:
                return {"action": "UPDATE", "fact_key": key, "score": 1.0, "match": "key"}
        # Fuzzy is a fallback for free-text only. Pointer stubs are boilerplate-
        # heavy and would steal the wrong key, so we trust derive_key for them.
        if not is_pointer(text):
            best, best_score = None, 0.0
            for r in rows:
                s = jaccard(text, r["content"])
                if s > best_score:
                    best, best_score = r["fact_key"], s
            if best is not None and best_score >= threshold:
                return {"action": "UPDATE", "fact_key": best, "score": round(best_score, 3), "match": "fuzzy"}
        return {"action": "NEW", "fact_key": key, "score": 0.0}

    # -- pull-mode reconciliation ---------------------------------------- #
    def ingest_files(self, stores: list[str], *, source: str = "ingest",
                     actor: str | None = None) -> dict:
        """Reconcile current file state vs last-known versions for each store."""
        summary = {"created": 0, "updated": 0, "duplicate": 0, "archived": 0,
                   "deleted": 0, "by_store": {}}
        for store in stores:
            path = self.memories_dir / store
            with self._lock(_lockpath(path)):     # observe a quiescent file
                entries = self._read_file_entries(path)
            assigned: dict[str, str] = {}
            touched: set[str] = set()
            st = {"created": 0, "updated": 0, "duplicate": 0, "archived": 0, "deleted": 0}
            prev_current = {r["fact_key"] for r in self.conn.execute(
                "SELECT fact_key FROM versions WHERE is_current=1 AND store=?", (store,)).fetchall()}
            for text in entries:
                m = self.match(text, store=store)
                key, action = m["fact_key"], m["action"]
                # within-pass collision: same derived key, different content placed
                if key in assigned and assigned[key] != content_hash(text):
                    n = 2
                    while f"{key}-{n}" in assigned:
                        n += 1
                    key = f"{key}-{n}"
                    action = "NEW"
                assigned[key] = content_hash(text)
                touched.add(key)
                op = {"NEW": "create", "UPDATE": "update", "DUPLICATE": "update"}[action]
                taxonomy = "navigation_pointer" if text.strip().startswith(HEADER_SENTINEL) else None
                res = self.record(fact_key=key, content=text, store=store, op=op,
                                  source=source, actor=actor, taxonomy=taxonomy,
                                  reason="pull-reconcile")
                if res is None:
                    st["duplicate"] += 1
                elif action == "NEW":
                    st["created"] += 1
                else:
                    st["updated"] += 1
            for key in prev_current - touched:
                arch = self._find_in_archives(key)
                if arch:
                    self.record(fact_key=key, content=arch["content"], store=store,
                                op="archive", source="curator",
                                valid_to=arch["archived"], archived_path=arch["path"],
                                taxonomy=arch.get("taxonomy"), reason=arch.get("reason"),
                                recorded_at=arch["archived"])
                    st["archived"] += 1
                else:
                    cur = self.conn.execute(
                        "SELECT content FROM versions WHERE fact_key=? AND is_current=1",
                        (key,)).fetchone()
                    self.record(fact_key=key, content=(cur["content"] if cur else f"[deleted] {key}"),
                                store=store, op="delete", source=source,
                                reason="vanished from file", allow_duplicate=True)
                    st["deleted"] += 1
            summary["by_store"][store] = st
            for k in ("created", "updated", "duplicate", "archived", "deleted"):
                summary[k] += st[k]
        return summary

    def _read_file_entries(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return []
        return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]

    # -- archive reconstruction ------------------------------------------ #
    # Anchor the next-block lookahead to a REAL block boundary ('## <slug>'
    # immediately followed by 'Status:'), so an inner '## ' in a body cannot
    # truncate/mis-split the block.
    _ARCH_BLOCK = re.compile(
        r"^## (?P<slug>.+?)\n"
        r"Status:\s*(?P<status>.+?)\n"
        r"Archived:\s*(?P<archived>\d{4}-\d{2}-\d{2}).*?\|\s*Source:\s*(?P<src>.+?)\s*"
        r"\|\s*Taxonomy:\s*(?P<tax>.+?)\s*\|\s*Hash:\s*(?P<hash>[0-9a-f]{16})\s*\n"
        r"Verdict:\s*(?P<verdict>.+?)\s*\|\s*Reason:\s*(?P<reason>.+?)\n"
        r"Backlink:\s*(?P<backlink>.+?)\n\n"
        r"(?P<body>.*?)(?=\n## .+\nStatus:|\Z)",
        re.DOTALL | re.MULTILINE)

    def _parse_archive_blocks(self) -> list[dict]:
        out = []
        if not self.archive_dir.is_dir():
            return out
        for f in sorted(self.archive_dir.glob("*-MEMORY.md")):
            txt = f.read_text(encoding="utf-8")
            for m in self._ARCH_BLOCK.finditer(txt):
                body = m.group("body").strip()
                if not body:
                    continue
                out.append({
                    "slug": m.group("slug").strip(), "archived": m.group("archived"),
                    "taxonomy": m.group("tax").strip(), "hash": m.group("hash"),
                    "reason": m.group("reason").strip(), "content": body,
                    "file": f.name,
                    "path": str(f).replace(str(Path.home()), "~"),
                })
        return out

    def _find_in_archives(self, fact_key: str) -> dict | None:
        """Best archive block whose derived key matches fact_key (latest archived)."""
        cands = [b for b in self._parse_archive_blocks() if derive_key(b["content"]) == fact_key]
        if not cands:
            return None
        return sorted(cands, key=lambda b: b["archived"])[-1]

    def ingest_archives(self, *, store: str = "MEMORY.md") -> dict:
        """Reconstruct prior full-content versions from curator archive blocks."""
        blocks = self._parse_archive_blocks()
        # Archive bodies are never "current" (they carry valid_to), so record()'s
        # current-only dedup can't make re-ingestion idempotent. Guard explicitly:
        # skip a body whose content_hash already exists anywhere in its fact's history.
        existing: dict[str, set] = {}
        for e in self._read_events():
            existing.setdefault(e["fact_key"], set()).add(e.get("content_hash"))
        recorded = 0
        for b in blocks:
            key = derive_key(b["content"])
            chash = content_hash(b["content"])
            if chash in existing.get(key, set()):
                continue  # idempotent: this archived version is already on record
            vf = latest_date(b["content"])
            res = self.record(
                fact_key=key, content=b["content"], store=store, op="create",
                source="curator", taxonomy=b["taxonomy"], valid_from=vf,
                valid_to=parse_dt(b["archived"]), recorded_at=parse_dt(b["archived"]),
                reason=b["reason"], archived_path=b["path"], allow_duplicate=True)
            if res is not None:
                existing.setdefault(key, set()).add(chash)
                recorded += 1
        # compute linkage from the FINAL materialized state (order-independent):
        # a fact is "linked" if it has both a curator-sourced version and a live one.
        linked = self.conn.execute(
            "SELECT COUNT(*) n FROM (SELECT fact_key FROM versions "
            "GROUP BY fact_key HAVING SUM(source='curator')>0 AND SUM(is_current=1)>0)").fetchone()["n"]
        arch_facts = self.conn.execute(
            "SELECT COUNT(DISTINCT fact_key) n FROM versions WHERE source='curator'").fetchone()["n"]
        return {"blocks": len(blocks), "recorded": recorded,
                "linked": linked, "standalone": arch_facts - linked}

    # -- queries ---------------------------------------------------------- #
    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        if isinstance(d.get("tags"), str):
            try:
                d["tags"] = json.loads(d["tags"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def current(self, *, store: str | None = None, key: str | None = None) -> list[dict]:
        q = "SELECT * FROM versions WHERE is_current=1"
        args: list = []
        if store:
            q += " AND store=?"; args.append(store)
        if key:
            q += " AND fact_key=?"; args.append(key)
        q += " ORDER BY store, fact_key"
        return [self._row(r) for r in self.conn.execute(q, args).fetchall()]

    def history(self, key: str) -> list[dict]:
        return [self._row(r) for r in self.conn.execute(
            "SELECT * FROM versions WHERE fact_key=? ORDER BY version", (key,)).fetchall()]

    def point_in_time(self, date: str, *, key: str | None = None,
                      store: str | None = None, axis: str = "valid") -> list[dict]:
        t = parse_dt(date)
        if axis == "transaction":
            lo, hi = "recorded_at", "superseded_at"
        else:
            lo, hi = "eff_valid_from", "valid_to"   # eff_valid_from = COALESCE(valid_from, recorded_at)
        q = (f"SELECT * FROM versions WHERE tombstone=0 "
             f"AND ({lo} IS NULL OR {lo}<=?) AND ({hi} IS NULL OR {hi}>?)")
        args: list = [t, t]
        if store:
            q += " AND store=?"; args.append(store)
        if key:
            q += " AND fact_key=?"; args.append(key)
        rows = [self._row(r) for r in self.conn.execute(q, args).fetchall()]
        # one row per fact: the version whose interval covers t; on ties prefer the
        # most-recently-recorded / highest-version (a same-day correction wins).
        seen: dict[str, dict] = {}
        for r in rows:
            k = r["fact_key"]
            cand = ((r.get(lo) or ""), (r.get("recorded_at") or ""), r.get("version") or 0)
            if k not in seen:
                seen[k] = r
            else:
                inc = ((seen[k].get(lo) or ""), (seen[k].get("recorded_at") or ""), seen[k].get("version") or 0)
                if cand > inc:
                    seen[k] = r
        return sorted(seen.values(), key=lambda r: (r["store"], r["fact_key"]))

    def diff(self, key: str, date1: str, date2: str, *, axis: str = "valid") -> dict:
        a = self.point_in_time(date1, key=key, axis=axis)
        b = self.point_in_time(date2, key=key, axis=axis)
        va = a[0] if a else None
        vb = b[0] if b else None
        ca = va["content"] if va else ""
        cb = vb["content"] if vb else ""
        import difflib
        la = re.split(r"(?<=[.;|])\s+", ca) if ca else []
        lb = re.split(r"(?<=[.;|])\s+", cb) if cb else []
        ud = list(difflib.unified_diff(la, lb, fromfile=f"{key}@{date1}",
                                       tofile=f"{key}@{date2}", lineterm=""))
        return {
            "fact_key": key, "axis": axis,
            "from": {"date": parse_dt(date1), "version": va["version"] if va else None,
                     "content": ca, "valid_from": va.get("valid_from") if va else None,
                     "recorded_at": va.get("recorded_at") if va else None},
            "to": {"date": parse_dt(date2), "version": vb["version"] if vb else None,
                   "content": cb, "valid_from": vb.get("valid_from") if vb else None,
                   "recorded_at": vb.get("recorded_at") if vb else None},
            "changed": ca.strip() != cb.strip(),
            "unified_diff": ud,
        }

    # -- restore ---------------------------------------------------------- #
    @staticmethod
    def _has_delimiter(text: str) -> bool:
        return (ENTRY_DELIMITER in text) or any(ln.strip() == "§" for ln in text.splitlines())

    def restore(self, key: str, *, at: str | None = None, version: int | None = None,
                apply: bool = False) -> dict:
        """Return (and optionally splice back) a prior version's content. Non-
        destructive by default. With apply=True: lock-first, drift-check,
        identity-match the live entry by fact_key, validate the wire format, take
        a .bak.<epoch>, and atomically rewrite (mode + symlink preserved)."""
        rows = self.history(key)
        if not rows:
            raise SystemExit(f"no history for fact_key={key!r}")
        if version is not None:
            target = next((r for r in rows if r["version"] == version), None)
        elif at is not None:
            pit = self.point_in_time(at, key=key)
            target = pit[0] if pit else None
        else:
            target = rows[-1]
        if target is None:
            raise SystemExit(f"no version of {key!r} matches the selector")
        result = {"fact_key": key, "version": target["version"], "store": target["store"],
                  "content": target["content"], "valid_from": target.get("valid_from"),
                  "valid_to": target.get("valid_to"), "applied": False}
        if not apply:
            return result

        new_text = target["content"]
        if self._has_delimiter(new_text):
            raise SystemExit("refusing to restore: content contains the § entry delimiter")
        if len(new_text) > PER_ENTRY_LIMIT:
            raise SystemExit(f"refusing to restore: content exceeds per-entry limit ({len(new_text)}>{PER_ENTRY_LIMIT})")
        path = self.memories_dir / target["store"]
        real = Path(os.path.realpath(path))      # resolve symlink so we replace the real target
        with self._lock(_lockpath(path)):
            raw = path.read_text(encoding="utf-8") if path.exists() else ""
            entries = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()] if raw.strip() else []
            # drift guard: file must be tool-shaped (round-trips cleanly)
            if raw.strip() and raw.strip() != ENTRY_DELIMITER.join(entries):
                raise SystemExit("refusing to restore: live file is not tool-shaped (external drift)")
            # identity match: replace the entry whose fact_key == key, else append
            replaced_idx = None
            for i, e in enumerate(entries):
                if derive_key(e) == key:
                    replaced_idx = i
                    break
            if replaced_idx is not None:
                entries[replaced_idx] = new_text
            else:
                entries.append(new_text)
            content = ENTRY_DELIMITER.join(entries)   # NO trailing newline (matches curator)
            mode = (real.stat().st_mode & 0o777) if real.exists() else 0o644
            if real.exists():
                bak = real.with_name(f"{real.name}.bak.{int(time.time())}")
                bak.write_bytes(real.read_bytes())
            fd, tmp = tempfile.mkstemp(dir=str(real.parent), suffix=".tmp", prefix=".mem_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content); f.flush(); os.fsync(f.fileno())
                os.chmod(tmp, mode)
                os.replace(tmp, real)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        self.record(fact_key=key, content=new_text, store=target["store"],
                    op="restore", source="restore",
                    reason=f"restored v{target['version']}", allow_duplicate=True)
        result["applied"] = True
        result["replaced"] = replaced_idx is not None
        return result

    # -- retention -------------------------------------------------------- #
    def prune(self, *, days: int = 90, keep_per_key: int = 10) -> dict:
        """Move stale superseded versions to the cold log (never hard-delete).
        Always keeps v1 (birth) and the current version of every fact. The whole
        read-compute-rewrite runs under the JSONL lock (no lost concurrent append),
        and the cold log is fsync'd BEFORE the hot log is shrunk (save-then-remove)."""
        cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).replace(microsecond=0).isoformat()
        with self._lock(_lockpath(self.jsonl)):
            events = self._read_events()                 # fresh read INSIDE the lock
            by_key: dict[str, list[dict]] = {}
            for ev in events:
                by_key.setdefault(ev["fact_key"], []).append(ev)
            keep_ids, cold = set(), []
            for key, evs in by_key.items():
                evs_sorted = sorted(evs, key=_txn_key)
                nk = len(evs_sorted)
                recent = {evs_sorted[j]["event_id"] for j in range(max(0, nk - keep_per_key), nk)}
                for i, ev in enumerate(evs_sorted):
                    is_birth = (i == 0)
                    is_current = (i == nk - 1)
                    old = (ev.get("recorded_at") or "") < cutoff
                    if is_birth or is_current or ev["event_id"] in recent or not old:
                        keep_ids.add(ev["event_id"])
                    else:
                        cold.append(ev)
            if not cold:
                return {"pruned": 0, "kept": len(keep_ids)}
            # 1) durably append to the cold log FIRST (never lose). Heal any torn
            #    last line first so this append can't glue onto it. (SAFETY-2)
            self._ensure_trailing_newline(self.cold_jsonl)
            with open(self.cold_jsonl, "a", encoding="utf-8") as f:
                for ev in cold:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                f.flush(); os.fsync(f.fileno())
            self._fsync_dir(self.cold_jsonl.parent)
            # 2) then rewrite the shrunken hot log atomically
            tmp = self.jsonl.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for ev in events:
                    if ev["event_id"] in keep_ids:
                        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.jsonl)
            self._fsync_dir(self.jsonl.parent)
        self.rebuild()
        return {"pruned": len(cold), "kept": len(keep_ids), "cold_log": str(self.cold_jsonl)}

    @staticmethod
    def _fsync_dir(d: Path) -> None:
        try:
            fd = os.open(str(d), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    # -- stats ------------------------------------------------------------ #
    def stats(self) -> dict:
        c = self.conn
        total = c.execute("SELECT COUNT(*) n FROM versions").fetchone()["n"]
        facts = c.execute("SELECT COUNT(DISTINCT fact_key) n FROM versions").fetchone()["n"]
        cur = c.execute("SELECT COUNT(*) n FROM versions WHERE is_current=1").fetchone()["n"]
        tomb = c.execute("SELECT COUNT(*) n FROM versions WHERE tombstone=1").fetchone()["n"]
        multi = c.execute(
            "SELECT COUNT(*) n FROM (SELECT fact_key FROM versions GROUP BY fact_key HAVING COUNT(*)>1)").fetchone()["n"]
        by_store = {r["store"]: r["n"] for r in c.execute(
            "SELECT store, COUNT(*) n FROM versions WHERE is_current=1 GROUP BY store").fetchall()}
        meta = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM meta").fetchall()}
        cold = (self.cold_jsonl.exists() and sum(
            1 for ln in _split_jsonl(self.cold_jsonl.read_text(encoding="utf-8", errors="replace")) if ln.strip())) or 0
        return {"total_versions": total, "facts": facts, "current_facts": cur,
                "facts_with_history": multi, "tombstoned": tomb,
                "current_by_store": by_store, "pruned_to_cold": cold,
                "read_warnings": len(self._read_warnings),
                "jsonl": str(self.jsonl),
                "jsonl_bytes": (self.jsonl.stat().st_size if self.jsonl.exists() else 0),
                "db": str(self.db_path), "meta": meta}


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def _emit(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
        return
    if isinstance(obj, list):
        for r in obj:
            if isinstance(r, dict) and "fact_key" in r:
                print(f"[{r.get('store','?')}] {r['fact_key']} v{r.get('version','?')} "
                      f"({r.get('op','?')}, valid_from={r.get('valid_from')}): "
                      f"{(r.get('content') or '')[:120]}")
            else:
                print(r)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    gopts = argparse.ArgumentParser(add_help=False)
    gopts.add_argument("--home", default=argparse.SUPPRESS,
                       help="HERMES_HOME override (default: env or ~/.hermes)")
    gopts.add_argument("--db", default=argparse.SUPPRESS, help="memory_versions.db path override")
    gopts.add_argument("--jsonl", default=argparse.SUPPRESS, help="history.jsonl path override")
    gopts.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="emit JSON")

    p = argparse.ArgumentParser(description="Temporal versioning for Hermes hot-memory.",
                                parents=[gopts])
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, help):
        return sub.add_parser(name, help=help, parents=[gopts])

    s = add("current", "current version(s)")
    s.add_argument("--store"); s.add_argument("--key")
    s = add("history", "all versions of a fact")
    s.add_argument("--key", required=True)
    s = add("at", "point-in-time snapshot")
    s.add_argument("--date", required=True); s.add_argument("--key"); s.add_argument("--store")
    s.add_argument("--time-axis", choices=["valid", "transaction"], default="valid")
    s = add("diff", "diff a fact between two times")
    s.add_argument("--key", required=True); s.add_argument("--from", dest="d1", required=True)
    s.add_argument("--to", dest="d2", required=True)
    s.add_argument("--time-axis", choices=["valid", "transaction"], default="valid")
    s = add("record", "append a version (write path)")
    s.add_argument("--key", required=True); s.add_argument("--content", required=True)
    s.add_argument("--store", default="MEMORY.md"); s.add_argument("--op", default="update")
    s.add_argument("--source", default="manual"); s.add_argument("--valid-from")
    s.add_argument("--valid-to"); s.add_argument("--confidence", type=float)
    s.add_argument("--reason"); s.add_argument("--actor")
    s = add("match", "NEW/UPDATE/DUPLICATE decision for text")
    s.add_argument("--text", required=True); s.add_argument("--store")
    s.add_argument("--threshold", type=float, default=0.6)
    s = add("ingest", "pull-reconcile current file state")
    s.add_argument("stores", nargs="*", default=["MEMORY.md", "USER.md"])
    s.add_argument("--source", default="ingest"); s.add_argument("--actor")
    add("ingest-archives", "reconstruct prior versions from curator archives")
    s = add("rebuild", "replay JSONL -> SQLite")
    s.add_argument("--force", action="store_true", help="rebuild even if the log appears to have vanished")
    add("stats", "store statistics")
    s = add("prune", "move stale superseded versions to cold log")
    s.add_argument("--days", type=int, default=90); s.add_argument("--keep-per-key", type=int, default=10)
    s = add("restore", "emit (or --apply) a prior version")
    s.add_argument("--key", required=True); s.add_argument("--at"); s.add_argument("--version", type=int)
    s.add_argument("--apply", action="store_true", help="splice back into MEMORY.md (.bak first)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tm = TemporalMemory(home=getattr(args, "home", None), db_path=getattr(args, "db", None),
                        jsonl_path=getattr(args, "jsonl", None))
    j = getattr(args, "json", False)
    if args.cmd == "current":
        _emit(tm.current(store=args.store, key=args.key), j)
    elif args.cmd == "history":
        _emit(tm.history(args.key), j)
    elif args.cmd == "at":
        _emit(tm.point_in_time(args.date, key=args.key, store=args.store, axis=args.time_axis), j)
    elif args.cmd == "diff":
        _emit(tm.diff(args.key, args.d1, args.d2, axis=args.time_axis), True)
    elif args.cmd == "record":
        res = tm.record(fact_key=args.key, content=args.content, store=args.store, op=args.op,
                        source=args.source, valid_from=args.valid_from, valid_to=args.valid_to,
                        confidence=args.confidence, reason=args.reason, actor=args.actor)
        _emit(res or {"action": "DUPLICATE", "fact_key": args.key}, j)
    elif args.cmd == "match":
        _emit(tm.match(args.text, store=args.store, threshold=args.threshold), j)
    elif args.cmd == "ingest":
        _emit(tm.ingest_files(args.stores, source=args.source, actor=args.actor), True)
    elif args.cmd == "ingest-archives":
        _emit(tm.ingest_archives(), True)
    elif args.cmd == "rebuild":
        _emit(tm.rebuild(force=args.force), True)
    elif args.cmd == "stats":
        _emit(tm.stats(), True)
    elif args.cmd == "prune":
        _emit(tm.prune(days=args.days, keep_per_key=args.keep_per_key), True)
    elif args.cmd == "restore":
        _emit(tm.restore(args.key, at=args.at, version=args.version, apply=args.apply), True)
    else:  # pragma: no cover
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
