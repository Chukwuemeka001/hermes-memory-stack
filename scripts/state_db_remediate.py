#!/usr/bin/env python3
"""Hermes state.db remediation — Area 1 of the memory-stack onboarding pipeline.

A conservative, reusable tool to audit and (only on explicit request) clean up
bloated Hermes ``state.db`` session databases. New users adopting the memory
stack typically arrive with a database that has grown unbounded — see the
forensic report at ``~/.hermes/notes/hermes/state-db-forensics-2026-06-23.md``
and the skill ``state-db-bloat-forensics``. The confirmed root causes are:

  * ``auto_prune`` ships OFF, so rows are never deleted -> monotonic growth.
  * Two own-content FTS5 indexes (``messages_fts`` unicode61 +
    ``messages_fts_trigram``) each store a *full duplicate* of every message;
    the trigram index alone can be ~49% of the file.
  * "Compression" is additive: the parent transcript is preserved
    (``end_reason='compression'``) and a summarized child session is inserted,
    so the file never shrinks.
  * ~85% of sessions never close (``ended_at IS NULL``), so age-based prune
    predicates that require a close time are no-ops.
  * ``VACUUM`` alone reclaims <1% — the fix must delete rows / drop indexes.

SAFETY MODEL (non-negotiable):
  * DRY-RUN is the default. ``audit``/``plan``/``simulate`` never modify any DB.
  * ``apply`` is the only mutating mode and refuses without ``--confirm-apply``.
  * Every destructive option is OFF unless the policy explicitly enables it
    (drop trigram, prune unclosed, delete compression parents, ...).
  * ``apply`` ALWAYS archives the original (db + WAL + SHM) with SHA-256 hashes
    BEFORE touching anything, and writes a restore helper.
  * Cleanup runs on a COPY first; ``PRAGMA integrity_check`` +
    ``foreign_key_check`` + an FTS-health check must pass before the cleaned
    copy atomically replaces the original. On any failure it aborts and points
    at the archive.
  * The liveness guard detects only a *currently-held write lock* and a pending
    WAL/journal; a running but idle gateway is NOT fully detectable. **Stop the
    gateway before applying to a live profile.**

stdlib only. Works for any Hermes user via ``--home`` / ``--db``.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections import defaultdict

TOOL_VERSION = "1.1.0"
SQLITE_MAGIC = b"SQLite format 3\x00"
# FTS5 shadow-table suffixes (the virtual table owns these behind the scenes).
FTS_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")
WORK_PREFIX = ".remediate_work_"
DEFAULT_DORMANT_DAYS = 14
DEFAULT_PROTECT_RECENT_DAYS = 2
SECONDS_PER_DAY = 86400.0
# Test-only hook: force the post-clean integrity check to "fail" so the abort/
# rollback path can be exercised without physically corrupting a database.
_FORCE_INTEGRITY_FAIL_ENV = "HERMES_REMEDIATE_FORCE_INTEGRITY_FAIL"


# --------------------------------------------------------------------------- #
# Small pure helpers                                                          #
# --------------------------------------------------------------------------- #
def now_ts() -> float:
    return time.time()


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def iso_compact(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M%S")


def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024.0
    return f"{n:.1f}PB"


def sha256_file(path: str, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def is_sqlite_file(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def ident(name: str) -> str:
    """Safely quote a SQL identifier (defends against odd table names)."""
    return '"' + name.replace('"', '""') + '"'


# --------------------------------------------------------------------------- #
# Read-only connection helpers                                                #
# --------------------------------------------------------------------------- #
def _ro_uri(path: str, immutable: bool = False) -> str:
    url = urllib.request.pathname2url(os.path.abspath(path))
    mode = "immutable=1" if immutable else "mode=ro"
    return f"file:{url}?{mode}"


def connect_ro(path: str) -> tuple[sqlite3.Connection, bool]:
    """Open a database read-only.

    Tries ``mode=ro`` first (safe even while another process writes the WAL).
    Falls back to ``immutable=1`` only if ``mode=ro`` cannot open the file
    (e.g. a quiescent WAL DB with no holder and a read-only filesystem) —
    that fallback is reported so the caller can note potentially stale reads.
    Never opens for write. Returns (connection, used_immutable_fallback).
    """
    try:
        con = sqlite3.connect(_ro_uri(path, immutable=False), uri=True, timeout=10.0)
        con.execute("SELECT 1")  # force open now so we can catch failures here
        con.row_factory = sqlite3.Row
        return con, False
    except sqlite3.OperationalError:
        con = sqlite3.connect(_ro_uri(path, immutable=True), uri=True, timeout=10.0)
        con.row_factory = sqlite3.Row
        return con, True


# --------------------------------------------------------------------------- #
# Schema introspection (everything below tolerates schema variance)          #
# --------------------------------------------------------------------------- #
def table_names(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,)).fetchone() is not None


def columns(con: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({ident(table)})")]
    except sqlite3.Error:
        return []


def has_column(con: sqlite3.Connection, table: str, col: str) -> bool:
    return col in columns(con, table)


def fts_virtual_tables(con: sqlite3.Connection) -> list[dict]:
    """Return FTS virtual tables, flagging the trigram tokenizer.

    A table is FTS if its DDL uses ``fts3/4/5``. It is a trigram index if the
    DDL declares ``tokenize = 'trigram'`` (the authoritative signal — SQLite
    stores the CREATE VIRTUAL TABLE text verbatim) or, as a last-resort
    heuristic, the table name contains 'trigram'. (Note: for fts5 the ``_config``
    shadow stores only the index version, never the tokenizer, so it is not a
    reliable detection source and is intentionally not consulted.)
    """
    out: list[dict] = []
    rows = con.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND sql IS NOT NULL AND "
        "(sql LIKE '%USING fts5%' OR sql LIKE '%USING fts4%' OR sql LIKE '%USING fts3%')"
    ).fetchall()
    for name, sql in rows:
        sql_l = (sql or "").lower()
        is_trigram = "trigram" in sql_l or "trigram" in name.lower()
        out.append({"name": name, "is_trigram": is_trigram, "sql": sql})
    return out


def fts_shadow_tables(con: sqlite3.Connection, fts_name: str) -> list[str]:
    present = set(table_names(con))
    return [f"{fts_name}{suf}" for suf in FTS_SHADOW_SUFFIXES
            if f"{fts_name}{suf}" in present]


def triggers_referencing(con: sqlite3.Connection, fts_name: str) -> list[str]:
    """Triggers that MAINTAIN a specific FTS table (write to it).

    Matches on a whole-identifier ``INSERT INTO/DELETE FROM/UPDATE <name>`` in
    the trigger body, with a negative lookahead so ``messages_fts`` never
    matches ``messages_fts_trigram`` (or any longer-named index). This is the
    safety-critical distinction: dropping the trigram index must not remove the
    surviving word-index's maintenance triggers.
    """
    pat = re.compile(
        r'(?:INSERT\s+INTO|DELETE\s+FROM|UPDATE)\s+"?' + re.escape(fts_name) + r'"?(?![\w])',
        re.IGNORECASE)
    out = []
    for name, sql in con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL"):
        if pat.search(sql or ""):
            out.append(name)
    return out


def dbstat_available(con: sqlite3.Connection) -> bool:
    try:
        con.execute("SELECT name, pgsize FROM dbstat LIMIT 1").fetchall()
        return True
    except sqlite3.Error:
        return False


def logical_table_bytes(con: sqlite3.Connection, table: str) -> int:
    """Estimate a table's payload bytes by summing column lengths.

    Used when ``dbstat`` is unavailable (default macOS Python build).
    LENGTH() on a BLOB is exact bytes; on TEXT it is characters, so this is a
    lower-bound estimate for multibyte text — always labelled as an estimate.
    """
    cols = columns(con, table)
    if not cols:
        return 0
    expr = " + ".join(f"COALESCE(LENGTH({ident(c)}),0)" for c in cols)
    try:
        v = con.execute(f"SELECT COALESCE(SUM({expr}),0) FROM {ident(table)}").fetchone()[0]
        return int(v or 0)
    except sqlite3.Error:
        return 0


def object_sizes(con: sqlite3.Connection) -> tuple[dict[str, int], str]:
    """Return {object_name: bytes} and the method used ('dbstat'|'estimate')."""
    if dbstat_available(con):
        sizes: dict[str, int] = {}
        for name, total in con.execute(
                "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
            sizes[name] = int(total or 0)
        return sizes, "dbstat"
    sizes = {}
    for t in table_names(con):
        sizes[t] = logical_table_bytes(con, t)
    return sizes, "estimate"


def fts_footprint(sizes: dict[str, int], con: sqlite3.Connection,
                  fts_name: str) -> int:
    total = sizes.get(fts_name, 0)
    for suf in FTS_SHADOW_SUFFIXES:
        total += sizes.get(f"{fts_name}{suf}", 0)
    return total


def fts_health_check(con: sqlite3.Connection) -> dict:
    """After cleanup, verify every surviving FTS still maintained + in sync.

    Guards against a drop that collaterally removed another FTS's triggers
    (which integrity_check/foreign_key_check do NOT catch): a surviving FTS with
    no maintenance triggers silently stops indexing new rows. Also flags
    own-content FTS whose row count diverged from ``messages``.
    """
    res = {"ok": True, "issues": []}
    if not has_table(con, "messages"):
        return res
    msg_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    for f in fts_virtual_tables(con):
        name = f["name"]
        trg = triggers_referencing(con, name)
        if len(trg) < 2:  # expect insert + delete (+ update) maintenance
            res["ok"] = False
            res["issues"].append(
                f"{name}: {len(trg)} maintenance trigger(s) (expected >=2) — "
                "FTS would stop tracking message changes")
        try:
            cnt = con.execute(f"SELECT COUNT(*) FROM {ident(name)}").fetchone()[0]
            if cnt != msg_count:
                res["ok"] = False
                res["issues"].append(
                    f"{name}: row count {cnt} != messages {msg_count} (FTS desynced)")
        except sqlite3.Error as e:
            res["issues"].append(f"{name}: count failed ({e})")
    return res


# --------------------------------------------------------------------------- #
# Discovery & classification                                                  #
# --------------------------------------------------------------------------- #
def classify_role(home: str | None, db_path: str) -> tuple[str, str]:
    """Return (role, profile_label) for a state.db path.

    Path-structure based (``home`` accepted for compatibility, not required).
    role='profile' only when a real ``profiles/<name>/`` segment exists before
    the filename; 'snapshot' for any ``state-snapshots``/``pre-update`` segment;
    otherwise 'default'.
    """
    p = os.path.abspath(db_path)
    parts = p.split(os.sep)
    low = p.lower()
    snapshot = ("state-snapshots" in low) or ("pre-update" in low)
    if "profiles" in parts:
        i = parts.index("profiles")
        if i + 1 <= len(parts) - 2:  # a <name> segment exists before the file
            profile = parts[i + 1]
            return ("snapshot", f"{profile}:snapshot") if snapshot else ("profile", profile)
    if snapshot:
        return "snapshot", "(default):snapshot"
    return "default", "(default)"


def _has_work_segment(path: str) -> bool:
    return any(seg.startswith(WORK_PREFIX) or seg.startswith(".remediate_staged_")
               for seg in path.split(os.sep))


def discover_state_dbs(home: str, follow_symlinks: bool = False) -> tuple[list[str], list[str]]:
    """Find every ``state.db`` under ``home``.

    Returns (db_paths, symlink_notes). Symlinked directories are NOT traversed
    by default and any encountered are reported. The tool's own working
    directories (``.remediate_work_*``) are skipped so a leftover/in-flight copy
    is never surfaced as a remediation target.
    """
    home = os.path.abspath(home)
    found: list[str] = []
    notes: list[str] = []
    for dirpath, dirnames, filenames in os.walk(home, followlinks=follow_symlinks):
        kept = []
        for d in dirnames:
            if d.startswith(WORK_PREFIX):
                continue  # never descend into our own work dirs
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                target = os.path.realpath(full)
                notes.append(f"symlink dir: {full} -> {target} "
                             f"({'followed' if follow_symlinks else 'NOT followed'})")
                if not follow_symlinks:
                    continue
            kept.append(d)
        dirnames[:] = kept
        if "state.db" in filenames:
            full = os.path.join(dirpath, "state.db")
            if _has_work_segment(full):
                continue
            if os.path.islink(full):
                notes.append(f"symlink file: {full} -> {os.path.realpath(full)} "
                             f"({'included' if follow_symlinks else 'SKIPPED'})")
                if not follow_symlinks:
                    continue
            found.append(full)
    found.sort()
    return found, notes


def sidecar_paths(db_path: str) -> list[str]:
    out = []
    for suf in ("-wal", "-shm", "-journal"):
        sc = db_path + suf
        if os.path.exists(sc):
            out.append(sc)
    return out


def pending_wal_bytes(db_path: str) -> int:
    """Bytes of uncheckpointed WAL/journal — the cheapest 'looks live' signal."""
    total = 0
    for suf in ("-wal", "-journal"):
        sc = db_path + suf
        try:
            if os.path.exists(sc):
                total += os.path.getsize(sc)
        except OSError:
            pass
    return total


def related_artifacts(db_path: str) -> list[dict]:
    """List sibling backup/orphan files for awareness (never targeted)."""
    out = []
    d = os.path.dirname(db_path)
    base = os.path.basename(db_path)
    try:
        for entry in sorted(os.listdir(d)):
            if entry == base:
                continue
            if entry.startswith(base + ".bak") or entry.startswith(base + ".pre") \
               or re.match(re.escape(base) + r"\.(bak|old|pre-remediation|backup)", entry):
                full = os.path.join(d, entry)
                if os.path.isfile(full):
                    out.append({"path": full, "bytes": os.path.getsize(full)})
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- #
# AUDIT (read-only)                                                           #
# --------------------------------------------------------------------------- #
def audit_db(db_path: str, home: str | None, dormant_days: int = DEFAULT_DORMANT_DAYS) -> dict:
    role, profile = classify_role(home, db_path)
    rec: dict = {
        "path": db_path,
        "profile": profile,
        "role": role,
        "exists": os.path.exists(db_path),
        "is_symlink": os.path.islink(db_path),
        "is_sqlite": False,
        "errors": [],
        "warnings": [],
    }
    if rec["is_symlink"]:
        rec["realpath"] = os.path.realpath(db_path)
        rec["warnings"].append(f"path is a symlink -> {rec['realpath']}")
    if not rec["exists"]:
        rec["errors"].append("file does not exist")
        return rec

    rec["file_bytes"] = os.path.getsize(db_path)
    rec["is_sqlite"] = is_sqlite_file(db_path)
    if not rec["is_sqlite"]:
        rec["errors"].append("not a SQLite database (bad magic header)")
        return rec

    sc = []
    for suf in ("-wal", "-shm", "-journal"):
        path = db_path + suf
        if os.path.exists(path):
            sc.append({"suffix": suf, "path": path, "bytes": os.path.getsize(path)})
    rec["sidecars"] = sc
    rec["sidecar_bytes"] = sum(s["bytes"] for s in sc)
    rec["has_uncheckpointed_wal"] = pending_wal_bytes(db_path) > 0
    rec["related_artifacts"] = related_artifacts(db_path)

    try:
        con, immutable = connect_ro(db_path)
    except sqlite3.Error as e:
        rec["errors"].append(f"cannot open read-only: {e}")
        return rec
    rec["used_immutable_fallback"] = immutable
    if immutable:
        rec["warnings"].append(
            "opened with immutable=1 fallback; metrics may be slightly stale if "
            "the DB is being written concurrently")

    try:
        def prag(name):
            try:
                return con.execute(f"PRAGMA {name}").fetchone()[0]
            except sqlite3.Error:
                return None
        rec["page_size"] = prag("page_size")
        rec["page_count"] = prag("page_count")
        rec["freelist_count"] = prag("freelist_count")
        rec["journal_mode"] = prag("journal_mode")
        rec["auto_vacuum"] = prag("auto_vacuum")
        ps = rec.get("page_size") or 0
        rec["freelist_bytes"] = int((rec.get("freelist_count") or 0) * ps)
        rec["sqlite_version"] = sqlite3.sqlite_version
        rec["dbstat_available"] = dbstat_available(con)

        schema_version = None
        if has_table(con, "schema_version"):
            try:
                row = con.execute("SELECT * FROM schema_version LIMIT 1").fetchone()
                if row is not None:
                    schema_version = row[0]
            except sqlite3.Error:
                pass
        if schema_version is None:
            schema_version = prag("user_version")
        rec["schema_version"] = schema_version

        rec["tables"] = table_names(con)
        ftss = fts_virtual_tables(con)
        rec["fts_tables"] = [f["name"] for f in ftss]
        rec["trigram_fts_tables"] = [f["name"] for f in ftss if f["is_trigram"]]

        rec["is_session_db"] = has_table(con, "sessions") and has_table(con, "messages")
        if not rec["is_session_db"]:
            rec["warnings"].append(
                "missing sessions/messages tables — not a Hermes session DB; "
                "audit-only, never remediated")

        sizes, size_method = object_sizes(con)
        rec["size_method"] = size_method
        rec["largest_objects"] = [
            {"name": n, "bytes": b}
            for n, b in sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)[:8]
        ]
        fts_total = trigram_total = 0
        per_fts = []
        for f in ftss:
            fp = fts_footprint(sizes, con, f["name"])
            per_fts.append({"name": f["name"], "is_trigram": f["is_trigram"], "bytes": fp})
            fts_total += fp
            if f["is_trigram"]:
                trigram_total += fp
        rec["fts_footprint"] = {"per_table": per_fts, "fts_total_bytes": fts_total,
                                "trigram_total_bytes": trigram_total, "method": size_method}

        if rec["is_session_db"]:
            _audit_session_metrics(con, rec, dormant_days)
        con.close()
    except sqlite3.Error as e:
        rec["errors"].append(f"audit query failed: {e}")
        try:
            con.close()
        except Exception:
            pass
    return rec


def _audit_session_metrics(con: sqlite3.Connection, rec: dict, dormant_days: int) -> None:
    now = now_ts()
    scols = columns(con, "sessions")
    mcols = columns(con, "messages")

    rec["sessions_count"] = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    rec["messages_count"] = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    has_ended = "ended_at" in scols
    has_started = "started_at" in scols
    has_end_reason = "end_reason" in scols
    has_parent = "parent_session_id" in scols
    has_source = "source" in scols

    if has_ended:
        rec["unclosed_sessions"] = con.execute(
            "SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()[0]
        rec["ended_sessions"] = con.execute(
            "SELECT COUNT(*) FROM sessions WHERE ended_at IS NOT NULL").fetchone()[0]
    else:
        rec["warnings"].append("sessions.ended_at column absent (older schema)")
        rec["unclosed_sessions"] = rec["ended_sessions"] = None

    if has_started:
        b = {"lt_7d": 0, "7_30d": 0, "30_90d": 0, "90_180d": 0, "gt_180d": 0,
             "no_timestamp": 0}
        t7, t30, t90, t180 = (now - 7 * SECONDS_PER_DAY, now - 30 * SECONDS_PER_DAY,
                              now - 90 * SECONDS_PER_DAY, now - 180 * SECONDS_PER_DAY)
        row = con.execute(
            "SELECT "
            " SUM(CASE WHEN started_at IS NULL THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN started_at < ? AND started_at >= ? THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN started_at < ? AND started_at >= ? THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN started_at < ? AND started_at >= ? THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN started_at < ? THEN 1 ELSE 0 END)"
            " FROM sessions",
            (t7, t7, t30, t30, t90, t90, t180, t180)).fetchone()
        for key, val in zip(("no_timestamp", "lt_7d", "7_30d", "30_90d", "90_180d", "gt_180d"), row):
            b[key] = val or 0
        rec["age_distribution"] = b
        mn, mx = con.execute("SELECT MIN(started_at), MAX(started_at) FROM sessions").fetchone()
        last_activity = mx
        if has_ended:
            e_mx = con.execute("SELECT MAX(ended_at) FROM sessions").fetchone()[0]
            if e_mx and (last_activity is None or e_mx > last_activity):
                last_activity = e_mx
        if "timestamp" in mcols:
            m_mx = con.execute("SELECT MAX(timestamp) FROM messages").fetchone()[0]
            if m_mx and (last_activity is None or m_mx > last_activity):
                last_activity = m_mx
        rec["oldest_session_started"] = iso(mn)
        rec["newest_session_started"] = iso(mx)
        rec["last_activity"] = iso(last_activity)
        if last_activity:
            age_days = (now - last_activity) / SECONDS_PER_DAY
            rec["days_since_activity"] = round(age_days, 1)
            rec["dormant"] = age_days > dormant_days
        else:
            rec["dormant"] = None
    else:
        rec["warnings"].append("sessions.started_at column absent (older schema)")
        rec["age_distribution"] = None
        rec["dormant"] = None

    if has_source:
        rec["sources"] = {str(s): c for s, c in con.execute(
            "SELECT source, COUNT(*) FROM sessions GROUP BY source ORDER BY 2 DESC")}

    comp = {"detected_by": None, "total": 0, "with_child": 0, "without_child": 0,
            "eligible_message_bytes_estimate": 0}
    if has_end_reason:
        comp["detected_by"] = "end_reason='compression'"
        comp["total"] = con.execute(
            "SELECT COUNT(*) FROM sessions WHERE end_reason='compression'").fetchone()[0]
        if has_parent and comp["total"]:
            comp["with_child"] = con.execute(
                "SELECT COUNT(*) FROM sessions p WHERE p.end_reason='compression' "
                "AND EXISTS (SELECT 1 FROM sessions ch WHERE ch.parent_session_id = p.id)"
            ).fetchone()[0]
            comp["without_child"] = comp["total"] - comp["with_child"]
            if "content" in mcols:
                pieces = ["COALESCE(LENGTH(content),0)"]
                if "tool_calls" in mcols:
                    pieces.append("COALESCE(LENGTH(tool_calls),0)")
                if "tool_name" in mcols:
                    pieces.append("COALESCE(LENGTH(tool_name),0)")
                expr = " + ".join(pieces)
                val = con.execute(
                    f"SELECT COALESCE(SUM({expr}),0) FROM messages "
                    "WHERE session_id IN (SELECT p.id FROM sessions p "
                    "  WHERE p.end_reason='compression' AND EXISTS "
                    "  (SELECT 1 FROM sessions ch WHERE ch.parent_session_id = p.id))"
                ).fetchone()[0]
                comp["eligible_message_bytes_estimate"] = int(val or 0)
        else:
            comp["without_child"] = comp["total"]
    else:
        rec["warnings"].append(
            "sessions.end_reason column absent — compression parents cannot be "
            "detected on this schema")
    rec["compression_parents"] = comp

    if has_end_reason:
        rec["end_reasons"] = {("(null)" if er is None else str(er)): c
                              for er, c in con.execute(
            "SELECT end_reason, COUNT(*) FROM sessions GROUP BY end_reason ORDER BY 2 DESC")}

    fts = rec.get("fts_footprint", {})
    rec["reclaim_estimates"] = {
        "note": "Estimates only. Real numbers come from `simulate`. Actual file "
                "shrink requires VACUUM after row/index deletion.",
        "vacuum_only_bytes": rec.get("freelist_bytes", 0),
        "drop_trigram_bytes": fts.get("trigram_total_bytes", 0),
        "delete_compression_parents_base_bytes": comp.get("eligible_message_bytes_estimate", 0),
    }


# --------------------------------------------------------------------------- #
# POLICY                                                                      #
# --------------------------------------------------------------------------- #
POLICY_SCHEMA = "hermes-state-remediation-policy/1"
_DESTRUCTIVE_KEYS = ("prune_closed", "prune_unclosed", "delete_compression_parents",
                     "drop_trigram", "vacuum")


def default_policy() -> dict:
    return {
        "schema": POLICY_SCHEMA,
        "tool_version": TOOL_VERSION,
        "created_at": iso(now_ts()),
        "retention_days": None,
        "prune_closed": False,
        "prune_unclosed": False,
        "delete_compression_parents": False,
        "drop_trigram": False,
        "vacuum": False,
        "include_dormant_profiles": False,
        "include_snapshots": False,
        "protect_sources": [],
        "protect_recent_days": DEFAULT_PROTECT_RECENT_DAYS,
        "dormant_days": DEFAULT_DORMANT_DAYS,
        "notes": "Generated by `plan`. See _help and skills/state-db-remediation.md "
                 "before editing — booleans below are DESTRUCTIVE.",
    }


def _yesno(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("yes", "y", "true", "1", "on")


def policy_from_args(args) -> dict:
    p = default_policy()
    p["retention_days"] = args.retention_days
    for attr, key in (("prune_closed", "prune_closed"),
                      ("prune_unclosed", "prune_unclosed"),
                      ("delete_compression_parents", "delete_compression_parents"),
                      ("drop_trigram", "drop_trigram"),
                      ("vacuum", "vacuum"),
                      ("include_dormant", "include_dormant_profiles"),
                      ("include_snapshots", "include_snapshots")):
        v = getattr(args, attr, None)
        if v is not None:
            p[key] = _yesno(v)
    if getattr(args, "protect_sources", None):
        p["protect_sources"] = [s.strip() for s in args.protect_sources.split(",") if s.strip()]
    if getattr(args, "protect_recent_days", None) is not None:
        p["protect_recent_days"] = args.protect_recent_days
    if getattr(args, "dormant_days", None) is not None:
        p["dormant_days"] = args.dormant_days
    if getattr(args, "notes", None):
        p["notes"] = args.notes
    return p


def validate_policy(p: dict) -> list[str]:
    errs: list[str] = []
    if p.get("schema") != POLICY_SCHEMA:
        errs.append(f"policy schema must be {POLICY_SCHEMA!r}, got {p.get('schema')!r}")
    wants_prune = p.get("prune_closed") or p.get("prune_unclosed")
    rd = p.get("retention_days")
    if wants_prune:
        if rd is None:
            errs.append("retention_days is required when pruning is enabled")
        elif not isinstance(rd, int) or rd < 1:
            errs.append("retention_days must be a positive integer")
    for key in ("prune_closed", "prune_unclosed", "delete_compression_parents",
                "drop_trigram", "vacuum", "include_dormant_profiles", "include_snapshots"):
        if not isinstance(p.get(key), bool):
            errs.append(f"policy.{key} must be a boolean")
    if not isinstance(p.get("protect_sources", []), list):
        errs.append("policy.protect_sources must be a list")
    prd = p.get("protect_recent_days", DEFAULT_PROTECT_RECENT_DAYS)
    if not isinstance(prd, int) or prd < 0:
        errs.append("policy.protect_recent_days must be a non-negative integer")
    return errs


def policy_warnings(p: dict) -> list[str]:
    w: list[str] = []
    rd = p.get("retention_days")
    if (p.get("prune_closed") or p.get("prune_unclosed")) and isinstance(rd, int) and rd < 7:
        w.append(f"retention_days={rd} is aggressive (<7); recent work may be deleted")
    if p.get("prune_unclosed"):
        w.append("prune_unclosed=yes will delete sessions that never closed — these "
                 "may be resumable/abandoned work. Age is measured from LAST ACTIVITY "
                 "(latest message), so still-active long-lived sessions are protected.")
    if p.get("delete_compression_parents"):
        w.append("delete_compression_parents=yes permanently drops ORIGINAL "
                 "transcripts (only the summarized child remains). A parent whose "
                 "only summary child would also be pruned is KEPT to avoid erasing "
                 "the whole conversation. Recoverable only from the archive.")
    if p.get("drop_trigram"):
        w.append("drop_trigram=yes removes substring/typo-tolerant search; "
                 "word-level (unicode61) search remains.")
    if p.get("include_snapshots"):
        w.append("include_snapshots=yes lets pre-update BACKUP snapshots be targeted "
                 "— these are your rollback net.")
    if not p.get("vacuum") and any(p.get(k) for k in _DESTRUCTIVE_KEYS if k != "vacuum"):
        w.append("vacuum=no: rows/indexes are removed but the file will NOT shrink "
                 "until a VACUUM runs (space becomes free pages, reused later).")
    return w


def is_noop_policy(p: dict) -> bool:
    return not any(p.get(k) for k in _DESTRUCTIVE_KEYS)


def load_policy(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            p = json.load(fh)
    except json.JSONDecodeError as e:
        # UX-3: a hand-edited / truncated policy must give an actionable message,
        # not a raw JSONDecodeError traceback.
        print(f"error: policy file {path} is not valid JSON ({e}).\n"
              f"       Expected the JSON written by `state_db_remediate.py plan --out {path}` "
              f"(check for a truncated copy or a trailing comma).", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(p, dict):
        print(f"error: policy file {path} must contain a JSON object (got {type(p).__name__}).",
              file=sys.stderr)
        raise SystemExit(2)
    base = default_policy()
    base.update({k: v for k, v in p.items() if k in base or k in (
        "schema", "tool_version", "created_at", "notes")})
    return base


POLICY_HELP = {
    "prune_closed": "Deletes CLOSED sessions older than retention_days (by last activity).",
    "prune_unclosed": "DELETES sessions that never closed (resumable/abandoned work). "
                      "Requires retention_days. Age = last message timestamp.",
    "delete_compression_parents": "PERMANENTLY drops ORIGINAL transcripts; only the "
                                  "summarized child remains. Parents whose only summary "
                                  "would also be pruned are kept. Archive-only recovery.",
    "drop_trigram": "Removes substring/typo-tolerant search; word-level search remains.",
    "vacuum": "false = no disk reclaim; freed rows become reusable pages, file size unchanged.",
    "include_dormant_profiles": "Plan/clean idle profiles (idle > dormant_days).",
    "include_snapshots": "Allow targeting pre-update BACKUP snapshots (your rollback net).",
    "protect_sources": "Session 'source' values to NEVER prune (e.g. ['telegram']).",
    "protect_recent_days": "Never touch sessions active within N days, regardless of close state.",
    "retention_days": "Required when any prune_* is true; sessions active more recently are kept.",
}


# --------------------------------------------------------------------------- #
# TARGET COLLECTION (what a policy would delete) — read-only                  #
# --------------------------------------------------------------------------- #
def collect_targets(con: sqlite3.Connection, policy: dict, now: float | None = None) -> dict:
    """Compute, read-only, the exact session ids a policy would delete and why.

    Pure analysis: never mutates. Used by `plan` (read-only on the live DB) and
    by the cleanup engine (on a COPY) so the two can never diverge.

    Key safety properties:
      * retention / protect_recent are measured from LAST ACTIVITY
        (latest message timestamp, else ended_at, else started_at), not merely
        the start time — an old session still receiving messages is protected.
      * a compression parent is deleted only if it has a child that SURVIVES
        this run; otherwise it is demoted (kept) so the conversation is never
        fully erased.
      * multi-level compression chains collapse at most ONE generation per run.
      * compression deletion is refused on schemas lacking started_at (cannot
        honour protect_recent_days there).
    """
    now = now or now_ts()
    scols = columns(con, "sessions")
    has_ended = "ended_at" in scols
    has_started = "started_at" in scols
    has_end_reason = "end_reason" in scols
    has_parent = "parent_session_id" in scols
    has_source = "source" in scols

    protect_sources = set(policy.get("protect_sources") or [])
    protect_recent_days = max(0, policy.get("protect_recent_days", DEFAULT_PROTECT_RECENT_DAYS))
    protect_recent_cut = now - protect_recent_days * SECONDS_PER_DAY
    rd = policy.get("retention_days")
    retention_cut = None if rd is None else now - rd * SECONDS_PER_DAY
    warnings: list[str] = []

    def last_act() -> str:
        parts = ["(SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id)"]
        if has_ended:
            parts.append("s.ended_at")
        parts.append("s.started_at")
        return "COALESCE(" + ", ".join(parts) + ")"

    def src_guard() -> tuple[str, list]:
        if has_source and protect_sources:
            qs = ",".join("?" for _ in protect_sources)
            return f" AND (s.source IS NULL OR s.source NOT IN ({qs}))", list(protect_sources)
        return "", []

    prune_closed: set = set()
    prune_unclosed: set = set()
    can_prune = has_ended and has_started
    if (policy.get("prune_closed") or policy.get("prune_unclosed")) and not can_prune:
        warnings.append("prune requested but schema lacks started_at/ended_at; skipped")
    if policy.get("prune_closed") and retention_cut is not None and can_prune:
        la = last_act()
        sg, sargs = src_guard()
        rows = con.execute(
            f"SELECT s.id FROM sessions s WHERE s.ended_at IS NOT NULL "
            f"AND {la} < ? AND {la} < ?{sg}",
            [retention_cut, protect_recent_cut] + sargs).fetchall()
        prune_closed = {r[0] for r in rows}
    if policy.get("prune_unclosed") and retention_cut is not None and can_prune:
        la = last_act()
        sg, sargs = src_guard()
        rows = con.execute(
            f"SELECT s.id FROM sessions s WHERE s.ended_at IS NULL "
            f"AND {la} < ? AND {la} < ?{sg}",
            [retention_cut, protect_recent_cut] + sargs).fetchall()
        prune_unclosed = {r[0] for r in rows}
    prune_ids = prune_closed | prune_unclosed

    comp_delete: set = set()
    comp_demoted = comp_chain_deferred = comp_refused_no_started = 0
    if policy.get("delete_compression_parents"):
        if not (has_end_reason and has_parent):
            warnings.append("delete_compression_parents requested but schema lacks "
                            "end_reason/parent_session_id; skipped")
        elif not has_started:
            comp_refused_no_started = con.execute(
                "SELECT COUNT(*) FROM sessions WHERE end_reason='compression'").fetchone()[0]
            warnings.append("delete_compression_parents skipped on this schema: no "
                            "started_at column, so protect_recent_days cannot be "
                            "enforced (refusing to delete possibly-recent originals)")
        else:
            comp_rows = con.execute(
                "SELECT id, parent_session_id, started_at, source FROM sessions "
                "WHERE end_reason='compression'").fetchall()
            comp_ids = {r[0] for r in comp_rows}
            children_of = defaultdict(list)
            for cid, pid in con.execute(
                    "SELECT id, parent_session_id FROM sessions "
                    "WHERE parent_session_id IS NOT NULL"):
                children_of[pid].append(cid)
            for pid, parent_of_p, started, source in comp_rows:
                if started is None or not (started < protect_recent_cut):
                    continue  # too recent / unknown start -> protected
                if has_source and source in protect_sources:
                    continue
                kids = children_of.get(pid, [])
                if not kids:
                    continue  # no summary child -> never auto-delete (nothing replaces it)
                if parent_of_p is not None and parent_of_p in comp_ids:
                    comp_chain_deferred += 1  # collapse one generation per run
                    continue
                if not any(k not in prune_ids for k in kids):
                    comp_demoted += 1  # only child(ren) being pruned -> keep parent
                    continue
                comp_delete.add(pid)
    if comp_demoted:
        warnings.append(f"{comp_demoted} compression parent(s) KEPT because their only "
                        "summary child would also be pruned (would erase the whole "
                        "conversation)")
    if comp_chain_deferred:
        warnings.append(f"{comp_chain_deferred} compression parent(s) deferred to a "
                        "future run (multi-level chain; one generation collapses per run)")

    all_ids = prune_ids | comp_delete

    msg_count = 0
    if all_ids:
        ids = list(all_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            qs = ",".join("?" for _ in chunk)
            msg_count += con.execute(
                f"SELECT COUNT(*) FROM messages WHERE session_id IN ({qs})", chunk).fetchone()[0]

    children_unlinked = 0
    if has_parent and all_ids:
        for cid, pid in con.execute(
                "SELECT id, parent_session_id FROM sessions WHERE parent_session_id IS NOT NULL"):
            if pid in all_ids and cid not in all_ids:
                children_unlinked += 1

    drop_fts = [f["name"] for f in fts_virtual_tables(con) if f["is_trigram"]] \
        if policy.get("drop_trigram") else []

    return {
        "session_ids": all_ids,
        "reasons": {"prune_closed": sorted(prune_closed),
                    "prune_unclosed": sorted(prune_unclosed),
                    "compression_parent": sorted(comp_delete)},
        "counts": {
            "sessions_to_delete": len(all_ids),
            "prune_closed": len(prune_closed),
            "prune_unclosed": len(prune_unclosed),
            "compression_parents": len(comp_delete),
            "messages_to_delete": msg_count,
            "children_unlinked": children_unlinked,
            "comp_parents_demoted_no_surviving_child": comp_demoted,
            "comp_parents_chain_deferred": comp_chain_deferred,
            "comp_parents_refused_no_started": comp_refused_no_started,
        },
        "drop_fts_tables": drop_fts,
        "retention_cutoff": iso(retention_cut) if retention_cut else None,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# CLEANUP ENGINE (operates on a COPY only)                                    #
# --------------------------------------------------------------------------- #
def integrity_report(con: sqlite3.Connection) -> dict:
    res = {"integrity_check": None, "foreign_key_violations": None, "ok": False}
    try:
        res["integrity_check"] = [r[0] for r in con.execute("PRAGMA integrity_check").fetchall()]
    except sqlite3.Error as e:
        res["integrity_check"] = [f"error: {e}"]
    try:
        res["foreign_key_violations"] = len(con.execute("PRAGMA foreign_key_check").fetchall())
    except sqlite3.Error as e:
        res["foreign_key_violations"] = f"error: {e}"
    res["ok"] = (res["integrity_check"] == ["ok"]) and (res["foreign_key_violations"] == 0)
    return res


def drop_fts_table(con: sqlite3.Connection, fts_name: str) -> dict:
    """Drop an FTS5 virtual table, its shadow tables, and ONLY its own triggers."""
    dropped_triggers = []
    for trg in triggers_referencing(con, fts_name):
        con.execute(f"DROP TRIGGER IF EXISTS {ident(trg)}")
        dropped_triggers.append(trg)
    try:
        con.execute(f"DROP TABLE IF EXISTS {ident(fts_name)}")
    except sqlite3.OperationalError:
        pass
    for suf in FTS_SHADOW_SUFFIXES:
        con.execute(f"DROP TABLE IF EXISTS {ident(fts_name + suf)}")
    return {"fts_table": fts_name, "dropped_triggers": dropped_triggers}


def run_cleanup_on_conn(con: sqlite3.Connection, policy: dict, now: float) -> dict:
    """Execute the policy's deletions/drops on an OPEN writable connection.

    The connection MUST point at a copy. Foreign keys must already be enabled by
    the caller (BEFORE the transaction began — the pragma is a no-op inside one).
    """
    stats: dict = {"deleted_sessions": 0, "deleted_messages": 0,
                   "children_unlinked": 0, "dropped_fts": [], "vacuumed": False}
    targets = collect_targets(con, policy, now=now)
    ids = list(targets["session_ids"])
    has_parent = has_column(con, "sessions", "parent_session_id")

    if ids:
        # 1) NULL every reference to a deleted session (kept children get
        #    unlinked; refs between two deleted sessions are cleared too, so FK
        #    enforcement cannot fail mid-batch). Guarded for legacy schemas.
        if has_parent:
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                qs = ",".join("?" for _ in chunk)
                con.execute(
                    f"UPDATE sessions SET parent_session_id=NULL "
                    f"WHERE parent_session_id IN ({qs})", chunk)
        # 2) delete messages (fires FTS delete triggers)
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            qs = ",".join("?" for _ in chunk)
            cur = con.execute(f"DELETE FROM messages WHERE session_id IN ({qs})", chunk)
            stats["deleted_messages"] += max(cur.rowcount, 0)
        # 3) delete the session rows
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            qs = ",".join("?" for _ in chunk)
            cur = con.execute(f"DELETE FROM sessions WHERE id IN ({qs})", chunk)
            stats["deleted_sessions"] += max(cur.rowcount, 0)

    stats["children_unlinked"] = targets["counts"]["children_unlinked"]
    if policy.get("drop_trigram"):
        for f in fts_virtual_tables(con):
            if f["is_trigram"]:
                stats["dropped_fts"].append(drop_fts_table(con, f["name"]))
    stats["target_counts"] = targets["counts"]
    stats["target_warnings"] = targets["warnings"]
    return stats


def search_impact_probe(con: sqlite3.Connection) -> dict:
    """Best-effort demonstration of what dropping trigram changes."""
    out = {"checked": False, "examples": []}
    ftss = fts_virtual_tables(con)
    trig = next((f["name"] for f in ftss if f["is_trigram"]), None)
    uni = next((f["name"] for f in ftss if not f["is_trigram"]), None)
    if not trig or not uni:
        return out
    out["checked"] = True
    try:
        rows = con.execute(
            "SELECT content FROM messages WHERE content IS NOT NULL "
            "AND LENGTH(content) > 12 LIMIT 50").fetchall()
        seen = set()
        for (content,) in rows:
            for word in re.findall(r"[A-Za-z]{6,}", content or ""):
                sub = word[1:5].lower()
                if len(sub) < 4 or sub in seen:
                    continue
                seen.add(sub)
                try:
                    t = con.execute(
                        f"SELECT COUNT(*) FROM {ident(trig)} WHERE {ident(trig)} MATCH ?",
                        (f'"{sub}"',)).fetchone()[0]
                    u = con.execute(
                        f"SELECT COUNT(*) FROM {ident(uni)} WHERE {ident(uni)} MATCH ?",
                        (f'"{sub}"',)).fetchone()[0]
                except sqlite3.Error:
                    continue
                if t > 0 and t > u:
                    out["examples"].append(
                        {"substring": sub, "trigram_hits": t, "unicode61_hits": u})
                if len(out["examples"]) >= 5:
                    return out
    except sqlite3.Error:
        pass
    return out


def clean_and_verify(db_path: str, policy: dict, workdir: str,
                     now: float | None = None) -> dict:
    """Copy the DB, run the policy on the copy, verify integrity + FTS health.

    NEVER touches ``db_path``. Produces a standalone single-file cleaned copy
    (no -wal/-shm) so it is safe to atomically swap in.
    """
    now = now or now_ts()
    os.makedirs(workdir, exist_ok=True)
    base = os.path.basename(db_path)
    work_db = os.path.join(workdir, base)

    shutil.copy2(db_path, work_db)
    for suf in ("-wal", "-shm"):
        if os.path.exists(db_path + suf):
            shutil.copy2(db_path + suf, work_db + suf)

    result: dict = {
        "source_db": db_path, "work_db": work_db,
        "before_bytes": os.path.getsize(db_path),
        "policy_targets": None, "cleanup_stats": None,
        "integrity_before": None, "integrity_after": None, "fts_health": None,
        "search_impact": None, "after_bytes": None,
        "aborted": False, "abort_reason": None, "row_counts": None,
    }

    con = sqlite3.connect(work_db, timeout=30.0)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA foreign_keys=ON")  # MUST be before BEGIN to take effect
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

        result["integrity_before"] = integrity_report(con)
        result["policy_targets"] = collect_targets(con, policy, now=now)["counts"]
        if policy.get("drop_trigram"):
            result["search_impact"] = search_impact_probe(con)

        before_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] \
            if has_table(con, "sessions") else None
        before_messages = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] \
            if has_table(con, "messages") else None

        con.execute("BEGIN")
        stats = run_cleanup_on_conn(con, policy, now)
        con.execute("COMMIT")
        result["cleanup_stats"] = stats

        if policy.get("vacuum"):
            con.execute("VACUUM")
            stats["vacuumed"] = True
        # Finalize to a standalone single file regardless of source journal mode.
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.Error:
            pass

        after = integrity_report(con)
        if os.environ.get(_FORCE_INTEGRITY_FAIL_ENV) == "1":
            after = {"integrity_check": ["forced-failure (test hook)"],
                     "foreign_key_violations": 0, "ok": False}
        result["integrity_after"] = after
        result["fts_health"] = fts_health_check(con)

        after_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] \
            if has_table(con, "sessions") else None
        after_messages = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] \
            if has_table(con, "messages") else None
        result["row_counts"] = {
            "sessions_before": before_sessions, "sessions_after": after_sessions,
            "messages_before": before_messages, "messages_after": after_messages,
        }
        con.close()
    except sqlite3.Error as e:
        try:
            con.rollback()
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass
        result["aborted"] = True
        result["abort_reason"] = f"SQL error during cleanup: {e}"
        return result

    # Ensure no sidecars cling to the cleaned copy before any swap.
    for suf in ("-wal", "-shm"):
        if os.path.exists(work_db + suf):
            try:
                os.remove(work_db + suf)
            except OSError:
                pass

    result["after_bytes"] = os.path.getsize(work_db)
    if not result["integrity_after"]["ok"]:
        result["aborted"] = True
        result["abort_reason"] = "post-clean integrity check failed"
    elif not result["fts_health"]["ok"]:
        result["aborted"] = True
        result["abort_reason"] = "FTS health check failed: " + \
            "; ".join(result["fts_health"]["issues"])
    return result


# --------------------------------------------------------------------------- #
# ARCHIVE + APPLY                                                             #
# --------------------------------------------------------------------------- #
ARCHIVE_SCHEMA = "hermes-state-remediation-archive/1"


def liveness_guard(db_path: str) -> tuple[bool, str]:
    """Best-effort, NON-MUTATING check that the DB is not actively held.

    1. A non-empty -wal/-journal sidecar => very likely live or killed
       mid-write => refuse.
    2. A read-only busy probe (never opens read-write, so it cannot checkpoint
       or delete the WAL) catches an exclusively-locked rollback-mode DB.

    LIMITATION (documented): a running but idle WAL-mode gateway holds no write
    lock and leaves no pending WAL between writes, so it is NOT detectable here.
    Always stop the gateway before applying to a live profile.
    """
    wal = pending_wal_bytes(db_path)
    if wal > 0:
        return False, (f"uncheckpointed WAL/journal present ({human_bytes(wal)}); the "
                       "DB is likely live or was killed mid-write. Stop the gateway "
                       "(let it checkpoint) or pass --allow-busy to override.")
    try:
        con = sqlite3.connect(_ro_uri(db_path, immutable=False), uri=True, timeout=2.5)
        con.execute("PRAGMA busy_timeout=2500")
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        con.close()
        return True, ("not busy (read-only probe). NOTE: a running idle gateway is "
                      "NOT detectable — stop Hermes before apply or writes will be lost.")
    except sqlite3.OperationalError as e:
        return False, (f"database appears to be in use ({e}); refusing. "
                       "Stop the gateway/holder first.")
    except sqlite3.Error as e:
        return False, f"could not verify liveness: {e}"


def archive_original(db_path: str, archive_dir: str, policy: dict) -> dict:
    """Tar the DB + sidecars with SHA-256 hashes BEFORE any modification."""
    ts = iso_compact(now_ts())
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.relpath(db_path, "/")).strip("_")[-80:]
    dest = os.path.join(archive_dir, f"state-db-remediation-{ts}-{slug}")
    os.makedirs(dest, exist_ok=True)

    members = [db_path] + sidecar_paths(db_path)
    files_meta = []
    tar_path = os.path.join(dest, "original.tar.gz")
    with tarfile.open(tar_path, "w:gz") as tar:
        for m in members:
            arc = os.path.basename(m)
            tar.add(m, arcname=arc)
            files_meta.append({"name": arc, "bytes": os.path.getsize(m),
                               "sha256": sha256_file(m)})
    with tarfile.open(tar_path, "r:gz") as tar:
        names = set(tar.getnames())
    missing = [f["name"] for f in files_meta if f["name"] not in names]
    if missing:
        raise RuntimeError(f"archive verification failed; missing members: {missing}")

    manifest = {
        "schema": ARCHIVE_SCHEMA, "tool_version": TOOL_VERSION,
        "created_at": iso(now_ts()), "original_db": os.path.abspath(db_path),
        "archive_tar": tar_path, "tar_sha256": sha256_file(tar_path),
        "files": files_meta, "policy": policy,
    }
    with open(os.path.join(dest, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    real = manifest["original_db"]
    d = os.path.dirname(real)
    restore_md = (
        f"# Restore — {os.path.basename(real)}\n\n"
        f"Archived: {manifest['created_at']}\n"
        f"Original: `{real}`\n\n"
        "To restore the ORIGINAL database (overwrites the current file):\n\n"
        "```bash\n"
        f"cd '{d}'\n"
        "# remove any current sidecars first so they cannot corrupt the restore\n"
        f"rm -f '{real}-wal' '{real}-shm' '{real}-journal'\n"
        f"tar xzf '{tar_path}' -C '{d}'\n"
        "```\n\n"
        "Verify with the SHA-256 hashes in `manifest.json`.\n"
    )
    with open(os.path.join(dest, "RESTORE.md"), "w", encoding="utf-8") as fh:
        fh.write(restore_md)

    manifest["archive_dir"] = dest
    return manifest


def apply_remediation(db_path: str, policy: dict, archive_dir: str,
                      allow_busy: bool = False, allow_snapshot: bool = False) -> dict:
    """The only mutating path. Resolve→guard→archive→clean-copy→verify→swap.

    The cleaned copy is always built inside the target's own directory so the
    final ``os.replace`` is an atomic, intra-filesystem rename; it is removed on
    every exit path.
    """
    now = now_ts()
    result: dict = {"db": db_path, "applied": False, "steps": [], "errors": [],
                    "policy_warnings": policy_warnings(policy)}

    # Resolve symlink so we archive/clean/swap the REAL file, never sever a link.
    if os.path.islink(db_path):
        real = os.path.realpath(db_path)
        result["steps"].append({"resolved_symlink": f"{db_path} -> {real}"})
        db_path = real
        result["db"] = db_path

    if not os.path.exists(db_path):
        result["errors"].append("database does not exist")
        return result
    if not is_sqlite_file(db_path):
        result["errors"].append("not a SQLite database")
        return result

    con, _imm = connect_ro(db_path)
    is_session = has_table(con, "sessions") and has_table(con, "messages")
    con.close()
    if not is_session:
        result["errors"].append("refusing: not a Hermes session DB (no sessions/messages tables)")
        return result

    role, _ = classify_role(None, db_path)
    if role == "snapshot" and not allow_snapshot:
        result["errors"].append(
            f"refusing: {db_path} is a pre-update BACKUP snapshot (role=snapshot), "
            "meant for rollback. Pass --allow-snapshot to override.")
        return result

    if is_noop_policy(policy):
        result["errors"].append(
            "refusing: policy is a no-op (no prune/drop/vacuum enabled). Enable at "
            "least one cleanup option, or run `audit`/`simulate` to inspect.")
        return result

    target_dir = os.path.dirname(os.path.abspath(db_path))

    # Liveness + WAL gate (non-mutating).
    if not allow_busy:
        ok, msg = liveness_guard(db_path)
        result["steps"].append({"liveness": msg})
        if not ok:
            result["errors"].append(msg)
            return result
    else:
        wal = pending_wal_bytes(db_path)
        result["steps"].append({"liveness": f"SKIPPED (--allow-busy); "
                                f"pending WAL/journal={human_bytes(wal)}"})

    # Snapshot of the original to detect concurrent writes (live gateway).
    try:
        orig_stat = (os.stat(db_path).st_mtime_ns, os.path.getsize(db_path))
    except OSError as e:
        result["errors"].append(f"cannot stat original: {e}")
        return result

    # Archive FIRST (liveness above is non-mutating, so the tar is faithful).
    try:
        manifest = archive_original(db_path, archive_dir, policy)
        result["archive"] = {"dir": manifest["archive_dir"], "tar": manifest["archive_tar"],
                             "files": manifest["files"]}
        result["steps"].append({"archived": manifest["archive_dir"]})
    except Exception as e:
        result["errors"].append(f"archive failed (no changes made): {e}")
        return result

    # Clean on a copy inside target_dir (guarantees same-filesystem atomic swap).
    wd = tempfile.mkdtemp(prefix=WORK_PREFIX, dir=target_dir)
    try:
        cleaned = clean_and_verify(db_path, policy, wd, now=now)
        result["clean"] = {k: cleaned.get(k) for k in (
            "before_bytes", "after_bytes", "cleanup_stats", "integrity_after",
            "fts_health", "row_counts", "aborted", "abort_reason", "policy_targets",
            "search_impact")}
        if cleaned["aborted"] or not (cleaned.get("integrity_after") or {}).get("ok"):
            result["errors"].append(
                f"cleanup/verify failed: {cleaned.get('abort_reason')}. ORIGINAL "
                f"UNCHANGED. Restore point: {manifest['archive_dir']}")
            return result
        result["steps"].append({"cleaned_and_verified": cleaned["work_db"]})

        # Race guard: ensure the original did not change while we worked.
        try:
            new_stat = (os.stat(db_path).st_mtime_ns, os.path.getsize(db_path))
        except OSError as e:
            new_stat = None
            result["errors"].append(f"cannot re-stat original before swap: {e}")
        if new_stat is not None and new_stat != orig_stat:
            result["errors"].append(
                "original changed during remediation (a live gateway is likely "
                "running). ORIGINAL UNCHANGED — refusing to swap a stale copy. "
                f"Stop Hermes and retry. Restore point: {manifest['archive_dir']}")
        if result["errors"]:
            return result

        # Atomic swap (same filesystem by construction).
        work_db = cleaned["work_db"]
        try:
            os.replace(work_db, db_path)
            result["steps"].append({"swapped": db_path})
        except OSError as e:
            result["errors"].append(
                f"atomic replace failed: {e}. ORIGINAL UNCHANGED. "
                f"Restore point: {manifest['archive_dir']}")
            return result

        # Remove now-stale original sidecars (already archived). Leaving them
        # would let SQLite replay the OLD WAL onto the NEW file and corrupt it.
        removed = []
        for suf in ("-wal", "-shm", "-journal"):
            sc = db_path + suf
            if os.path.exists(sc):
                try:
                    os.remove(sc)
                    removed.append(sc)
                except OSError:
                    pass
        result["steps"].append({"removed_stale_sidecars": removed})

        try:
            con, _ = connect_ro(db_path)
            post = integrity_report(con)
            con.close()
            result["post_swap_integrity"] = post
            if not post["ok"]:
                result["errors"].append(
                    f"post-swap integrity check FAILED — RESTORE from {manifest['archive_dir']}")
        except sqlite3.Error as e:
            result["errors"].append(f"post-swap verification error: {e}")

        result["after_bytes"] = os.path.getsize(db_path)
        result["reclaimed_bytes"] = cleaned["before_bytes"] - result["after_bytes"]
        result["applied"] = not result["errors"]

        try:
            manifest["apply_result"] = {
                "applied": result["applied"], "before_bytes": cleaned["before_bytes"],
                "after_bytes": result["after_bytes"],
                "reclaimed_bytes": result.get("reclaimed_bytes"),
                "cleanup_stats": cleaned["cleanup_stats"],
            }
            with open(os.path.join(manifest["archive_dir"], "manifest.json"),
                      "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, indent=2)
        except Exception:
            pass
        return result
    finally:
        _safe_rmtree(wd, target_dir)


def _safe_rmtree(path: str, must_be_under: str) -> None:
    """Remove a working dir only if it is safely under the target directory."""
    try:
        ap = os.path.abspath(path)
        base = os.path.abspath(must_be_under)
        if ap.startswith(base + os.sep) and os.path.isdir(ap) \
                and os.path.basename(ap).startswith(WORK_PREFIX):
            shutil.rmtree(ap, ignore_errors=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# HUMAN-READABLE RENDERING                                                    #
# --------------------------------------------------------------------------- #
def render_audit_human(records: list[dict], symlink_notes: list[str]) -> str:
    lines = ["=" * 72, "HERMES state.db AUDIT (read-only)", "=" * 72]
    total = sum(r.get("file_bytes", 0) for r in records)
    sc_total = sum(r.get("sidecar_bytes", 0) for r in records)
    lines.append(f"Databases found: {len(records)}   Total DB bytes: {human_bytes(total)}   "
                 f"Sidecars: {human_bytes(sc_total)}")
    if symlink_notes:
        lines.append("")
        lines.append("Symlinks (not followed unless --follow-symlinks):")
        for n in symlink_notes:
            lines.append(f"  ! {n}")
    for r in records:
        lines.append("")
        lines.append("-" * 72)
        flags = []
        if r.get("role"):
            flags.append(r["role"])
        if r.get("dormant"):
            flags.append("DORMANT")
        if r.get("has_uncheckpointed_wal"):
            flags.append("WAL-PENDING")
        if r.get("is_symlink"):
            flags.append("SYMLINK")
        if not r.get("is_session_db", True):
            flags.append("NON-SESSION")
        lines.append(f"{r['path']}")
        lines.append(f"  profile={r.get('profile')}  [{', '.join(flags)}]")
        if r.get("errors"):
            for e in r["errors"]:
                lines.append(f"  ERROR: {e}")
            continue
        lines.append(f"  size={human_bytes(r.get('file_bytes'))}  "
                     f"pages={r.get('page_count')}x{r.get('page_size')}  "
                     f"freelist={human_bytes(r.get('freelist_bytes'))}  "
                     f"journal={r.get('journal_mode')}  schema_v={r.get('schema_version')}")
        if r.get("sidecars"):
            lines.append("  sidecars: " + "  ".join(
                f"{s['suffix']}={human_bytes(s['bytes'])}" for s in r["sidecars"]))
        if r.get("is_session_db"):
            lines.append(f"  sessions={r.get('sessions_count')}  messages={r.get('messages_count')}  "
                         f"unclosed={r.get('unclosed_sessions')}  ended={r.get('ended_sessions')}")
            ad = r.get("age_distribution") or {}
            if ad:
                lines.append(f"  age: <7d={ad.get('lt_7d')}  7-30d={ad.get('7_30d')}  "
                             f"30-90d={ad.get('30_90d')}  90-180d={ad.get('90_180d')}  "
                             f">180d={ad.get('gt_180d')}")
            if r.get("last_activity"):
                lines.append(f"  last activity: {r['last_activity']} ({r.get('days_since_activity')}d ago)")
            if r.get("sources"):
                lines.append("  sources: " + "  ".join(f"{k}={v}" for k, v in r["sources"].items()))
            comp = r.get("compression_parents") or {}
            lines.append(f"  compression parents: total={comp.get('total')}  "
                         f"deletable(with child)={comp.get('with_child')}  "
                         f"keep(no child)={comp.get('without_child')}  [{comp.get('detected_by')}]")
        fts = r.get("fts_footprint") or {}
        if fts.get("per_table"):
            lines.append(f"  FTS footprint ({fts.get('method')}): "
                         f"total={human_bytes(fts.get('fts_total_bytes'))}  "
                         f"trigram={human_bytes(fts.get('trigram_total_bytes'))}")
            for t in fts["per_table"]:
                tag = " [trigram]" if t["is_trigram"] else ""
                pct = f" ({100*t['bytes']/r['file_bytes']:.0f}% of file)" if r.get("file_bytes") else ""
                lines.append(f"      {t['name']}{tag}: {human_bytes(t['bytes'])}{pct}")
        rec = r.get("reclaim_estimates") or {}
        if rec:
            lines.append(f"  reclaim estimates: vacuum-only={human_bytes(rec.get('vacuum_only_bytes'))}  "
                         f"drop-trigram={human_bytes(rec.get('drop_trigram_bytes'))}  "
                         f"comp-parents(base)={human_bytes(rec.get('delete_compression_parents_base_bytes'))}")
        for w in r.get("warnings", []):
            lines.append(f"  ~ {w}")
        for ra in r.get("related_artifacts", []):
            lines.append(f"  related (not targeted): {ra['path']} ({human_bytes(ra['bytes'])})")
    lines.append("")
    lines.append("Estimates only. Run `plan` then `simulate` for ground-truth numbers.")
    return "\n".join(lines)


def _render_counts(c: dict) -> list[str]:
    lines = [f"  would delete: sessions={c.get('sessions_to_delete')} "
             f"(closed={c.get('prune_closed')}, unclosed={c.get('prune_unclosed')}, "
             f"comp-parents={c.get('compression_parents')})  messages={c.get('messages_to_delete')}",
             f"  children unlinked: {c.get('children_unlinked')}"]
    extra = []
    if c.get("comp_parents_demoted_no_surviving_child"):
        extra.append(f"kept(no surviving summary)={c['comp_parents_demoted_no_surviving_child']}")
    if c.get("comp_parents_chain_deferred"):
        extra.append(f"chain-deferred={c['comp_parents_chain_deferred']}")
    if c.get("comp_parents_refused_no_started"):
        extra.append(f"refused(no started_at)={c['comp_parents_refused_no_started']}")
    if extra:
        lines.append("  compression safety: " + "  ".join(extra))
    return lines


def render_plan_human(plan_records: list[dict], policy: dict) -> str:
    lines = ["=" * 72, "REMEDIATION PLAN (dry-run; nothing executed)", "=" * 72, "Policy:"]
    for k in ("retention_days", "prune_closed", "prune_unclosed",
              "delete_compression_parents", "drop_trigram", "vacuum",
              "include_dormant_profiles", "include_snapshots", "protect_sources",
              "protect_recent_days"):
        lines.append(f"  {k} = {policy.get(k)}")
    for w in policy_warnings(policy):
        lines.append(f"  ! {w}")
    for pr in plan_records:
        lines.append("")
        lines.append("-" * 72)
        lines.append(f"{pr['path']}  (profile={pr.get('profile')}, role={pr.get('role')})")
        if pr.get("skipped"):
            lines.append(f"  SKIPPED: {pr['skipped']}")
            continue
        lines.extend(_render_counts(pr.get("counts", {})))
        if pr.get("drop_fts_tables"):
            lines.append(f"  would drop FTS: {', '.join(pr['drop_fts_tables'])}")
        if pr.get("retention_cutoff"):
            lines.append(f"  retention cutoff (by last activity): {pr['retention_cutoff']}")
        for w in pr.get("warnings", []):
            lines.append(f"  ! {w}")
    lines.append("")
    lines.append("Next: `simulate --db <path> --policy <file>` runs this on a COPY and "
                 "reports real before/after sizes + integrity.")
    return "\n".join(lines)


def _render_sim_human(result: dict, policy: dict, keep: bool, wd: str) -> str:
    lines = ["=" * 72, "SIMULATE — cleanup executed on a COPY (original untouched)", "=" * 72]
    lines.append(f"source: {result['source_db']}")
    lines.append(f"copy:   {result['work_db']}")
    for w in policy_warnings(policy):
        lines.append(f"  ! {w}")
    for w in (result.get("cleanup_stats") or {}).get("target_warnings", []):
        lines.append(f"  ! {w}")
    lines.append("")
    pt = result.get("policy_targets") or {}
    lines.extend(_render_counts(pt))
    st = result.get("cleanup_stats") or {}
    lines.append(f"applied on copy: sessions={st.get('deleted_sessions')}  "
                 f"messages={st.get('deleted_messages')}  "
                 f"children_unlinked={st.get('children_unlinked')}  "
                 f"dropped_fts={[d['fts_table'] for d in st.get('dropped_fts', [])]}  "
                 f"vacuumed={st.get('vacuumed')}")
    rc = result.get("row_counts") or {}
    lines.append(f"rows: sessions {rc.get('sessions_before')}->{rc.get('sessions_after')}  "
                 f"messages {rc.get('messages_before')}->{rc.get('messages_after')}")
    bb, ab = result.get("before_bytes"), result.get("after_bytes")
    if bb is not None and ab is not None:
        delta = bb - ab
        pct = (100 * delta / bb) if bb else 0
        lines.append(f"size: {human_bytes(bb)} -> {human_bytes(ab)}  "
                     f"(reclaimed {human_bytes(delta)}, {pct:.1f}%)")
    ia = result.get("integrity_after") or {}
    fh = result.get("fts_health") or {}
    lines.append(f"integrity AFTER: {'OK' if ia.get('ok') else 'FAILED'}  "
                 f"(fk_violations={ia.get('foreign_key_violations')})   "
                 f"FTS health: {'OK' if fh.get('ok') else 'FAILED'}")
    for issue in fh.get("issues", []):
        lines.append(f"    FTS: {issue}")
    si = result.get("search_impact")
    if si and si.get("checked"):
        if si["examples"]:
            lines.append("search impact (substrings that ONLY trigram matches):")
            for ex in si["examples"]:
                lines.append(f"    '{ex['substring']}': trigram={ex['trigram_hits']} "
                             f"unicode61={ex['unicode61_hits']}")
        else:
            lines.append("search impact: no substring-only matches sampled "
                         "(dropping trigram likely low-impact for this data)")
    if result.get("aborted"):
        lines.append(f"ABORTED: {result.get('abort_reason')}")
    lines.append("")
    lines.append(f"cleaned copy kept at: {wd}" if keep
                 else "(temporary copy will be deleted; pass --workdir to keep it)")
    return "\n".join(lines)


def _render_apply_human(result: dict) -> str:
    lines = ["=" * 72,
             "APPLY" + ("  ✓ SUCCESS" if result.get("applied") else "  ✗ NOT APPLIED"),
             "=" * 72, f"db: {result['db']}"]
    for w in result.get("policy_warnings", []):
        lines.append(f"  ! {w}")
    if result.get("archive"):
        lines.append(f"archive: {result['archive']['dir']}")
    for step in result.get("steps", []):
        for k, v in step.items():
            lines.append(f"  [{k}] {v}")
    cl = result.get("clean") or {}
    if cl:
        for w in (cl.get("cleanup_stats") or {}).get("target_warnings", []):
            lines.append(f"  ! {w}")
        bb, ab = cl.get("before_bytes"), cl.get("after_bytes")
        if bb is not None and ab is not None:
            lines.append(f"  size: {human_bytes(bb)} -> {human_bytes(ab)} (reclaimed {human_bytes(bb-ab)})")
        st = cl.get("cleanup_stats") or {}
        lines.append(f"  deleted: sessions={st.get('deleted_sessions')} messages={st.get('deleted_messages')}")
    if result.get("post_swap_integrity"):
        lines.append(f"  post-swap integrity: "
                     f"{'OK' if result['post_swap_integrity'].get('ok') else 'FAILED'}")
    for e in result.get("errors", []):
        lines.append(f"  ERROR: {e}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# COMMANDS                                                                    #
# --------------------------------------------------------------------------- #
def _emit(obj, args, human: str) -> None:
    rf = getattr(args, "report_file", None)
    if rf:  # create the parent dir so --report-file /new/dir/r.json just works
        os.makedirs(os.path.dirname(os.path.abspath(rf)) or ".", exist_ok=True)
    if getattr(args, "json", False):
        text = json.dumps(obj, indent=2, default=str)
        print(text)
        if rf:
            with open(rf, "w", encoding="utf-8") as fh:
                fh.write(text)
    else:
        print(human)
        if rf:
            with open(rf, "w", encoding="utf-8") as fh:
                fh.write(human)


def _resolve_db_arg(path: str) -> tuple[str, list[str]]:
    """Abspath + resolve a symlink, returning (realpath, notes)."""
    ap = os.path.abspath(path)
    notes = []
    if os.path.islink(ap):
        rp = os.path.realpath(ap)
        notes.append(f"symlink: {ap} -> {rp} (resolved to real path)")
        ap = rp
    return ap, notes


def _resolve_targets(args) -> tuple[list[str], list[str]]:
    if getattr(args, "db", None):
        dbs = args.db if isinstance(args.db, list) else [args.db]
        out, notes = [], []
        for d in dbs:
            ap, n = _resolve_db_arg(d)
            out.append(ap)
            notes.extend(n)
        if not getattr(args, "home", None):
            args.home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        return out, notes
    home = args.home or os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    args.home = home
    return discover_state_dbs(home, follow_symlinks=getattr(args, "follow_symlinks", False))


def cmd_audit(args) -> int:
    targets, symlink_notes = _resolve_targets(args)
    records = [audit_db(p, args.home, dormant_days=DEFAULT_DORMANT_DAYS) for p in targets]
    obj = {"tool_version": TOOL_VERSION, "generated_at": iso(now_ts()),
           "home": os.path.abspath(args.home) if args.home else None,
           "sqlite_version": sqlite3.sqlite_version,
           "symlink_notes": symlink_notes, "databases": records}
    _emit(obj, args, render_audit_human(records, symlink_notes))
    return 0


def cmd_plan(args) -> int:
    policy = policy_from_args(args)
    errs = validate_policy(policy)
    if errs:
        for e in errs:
            print(f"policy error: {e}", file=sys.stderr)
        return 2

    targets, _notes = _resolve_targets(args)
    plan_records = []
    for p in targets:
        rec = {"path": p}
        role, profile = classify_role(args.home, p)
        rec["role"], rec["profile"] = role, profile
        if not is_sqlite_file(p):
            rec["skipped"] = "not a SQLite database"
            plan_records.append(rec)
            continue
        try:
            con, _imm = connect_ro(p)
        except sqlite3.Error as e:
            rec["skipped"] = f"cannot open: {e}"
            plan_records.append(rec)
            continue
        try:
            if not (has_table(con, "sessions") and has_table(con, "messages")):
                rec["skipped"] = "not a Hermes session DB"
                con.close()
                plan_records.append(rec)
                continue
            if role == "snapshot" and not policy["include_snapshots"] and not args.db:
                rec["skipped"] = "snapshot/backup DB; protected by default (set --include-snapshots)"
                con.close()
                plan_records.append(rec)
                continue
            if role == "profile" and not policy["include_dormant_profiles"] and not args.db:
                aud = audit_db(p, args.home, dormant_days=policy["dormant_days"])
                if aud.get("dormant"):
                    rec["skipped"] = (f"dormant profile ({aud.get('days_since_activity')}d idle); "
                                      "set include_dormant=yes to include")
                    con.close()
                    plan_records.append(rec)
                    continue
            t = collect_targets(con, policy)
            rec["counts"] = t["counts"]
            rec["drop_fts_tables"] = t["drop_fts_tables"]
            rec["retention_cutoff"] = t["retention_cutoff"]
            rec["warnings"] = t["warnings"]
            con.close()
        except sqlite3.Error as e:
            rec["skipped"] = f"query error: {e}"
            try:
                con.close()
            except Exception:
                pass
        plan_records.append(rec)

    if getattr(args, "out", None):
        policy_out = dict(policy)
        policy_out["_README"] = ("Hermes state.db remediation policy. The booleans below "
                                 "are DESTRUCTIVE and default OFF. `apply` requires "
                                 "--confirm-apply. See skills/state-db-remediation.md.")
        policy_out["_help"] = POLICY_HELP
        # Create the parent dir so the golden-path `plan --out /tmp/new/policy.json`
        # just works instead of a bare errno + a silently-unwritten policy.
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(policy_out, fh, indent=2)
        if not getattr(args, "json", False):
            print(f"[wrote policy] {args.out}")

    obj = {"tool_version": TOOL_VERSION, "generated_at": iso(now_ts()),
           "policy": policy, "policy_warnings": policy_warnings(policy), "plans": plan_records}
    _emit(obj, args, render_plan_human(plan_records, policy))
    return 0


def cmd_simulate(args) -> int:
    if not args.db:
        print("simulate requires --db <path>", file=sys.stderr)
        return 2
    db, notes = _resolve_db_arg(args.db)
    for n in notes:
        print(f"[note] {n}", file=sys.stderr)
    policy = load_policy(args.policy)
    errs = validate_policy(policy)
    if errs:
        for e in errs:
            print(f"policy error: {e}", file=sys.stderr)
        return 2
    if not is_sqlite_file(db):
        print(f"not a SQLite database: {db}", file=sys.stderr)
        return 2
    role, _ = classify_role(None, db)
    if role == "snapshot" and not getattr(args, "allow_snapshot", False):
        print(f"[note] {db} is a snapshot/backup DB; simulating anyway (read-only on a copy).",
              file=sys.stderr)

    keep = bool(args.workdir)
    wd = args.workdir or tempfile.mkdtemp(prefix="hermes_remediate_sim_")
    result = clean_and_verify(db, policy, wd)
    result.update({"workdir": wd, "kept_workdir": keep,
                   "policy_warnings": policy_warnings(policy)})
    human = _render_sim_human(result, policy, keep, wd)
    _emit(result, args, human)
    if not keep:
        shutil.rmtree(wd, ignore_errors=True)
    ok = (result.get("integrity_after") or {}).get("ok") and \
         (result.get("fts_health") or {}).get("ok") and not result.get("aborted")
    return 0 if ok else 1


def cmd_apply(args) -> int:
    if not args.db:
        print("apply requires --db <path>", file=sys.stderr)
        return 2
    if not args.confirm_apply:
        print("REFUSING: apply requires --confirm-apply (this modifies a real DB).", file=sys.stderr)
        print("Run `simulate` first to preview on a copy.", file=sys.stderr)
        return 2
    if not args.archive_dir:
        print("apply requires --archive-dir (originals are archived before changes).", file=sys.stderr)
        return 2
    policy = load_policy(args.policy)
    errs = validate_policy(policy)
    if errs:
        for e in errs:
            print(f"policy error: {e}", file=sys.stderr)
        return 2

    # Surface destructive warnings BEFORE doing anything irreversible.
    for w in policy_warnings(policy):
        print(f"[warning] {w}", file=sys.stderr)

    db, notes = _resolve_db_arg(args.db)
    for n in notes:
        print(f"[note] {n}", file=sys.stderr)
    # NB: archive_dir is created lazily by archive_original only once we are past
    # all refuse-gates, so a refused apply (no-op/snapshot/busy/WAL) leaves no trace.
    result = apply_remediation(db, policy, args.archive_dir,
                               allow_busy=args.allow_busy,
                               allow_snapshot=args.allow_snapshot)
    _emit(result, args, _render_apply_human(result))
    return 0 if result.get("applied") else 1


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    epilog = (
        "SAFETY: audit/plan/simulate never modify any database. Only `apply` mutates, "
        "and only with --confirm-apply, after archiving the original and verifying a "
        "cleaned copy. The liveness guard cannot detect an idle running gateway — STOP "
        "Hermes before applying to a live profile. Typical flow:\n"
        "  1) audit    --home ~/.hermes --json\n"
        "  2) plan     --home ~/.hermes --retention-days 90 --prune-unclosed no "
        "--drop-trigram no --delete-compression-parents no --out /tmp/policy.json\n"
        "  3) simulate --db ~/.hermes/state.db --policy /tmp/policy.json --workdir /tmp/sim\n"
        "  4) apply    --db <stopped-db> --policy /tmp/policy.json "
        "--archive-dir ~/.hermes/archives/remediation --confirm-apply\n"
    )
    p = argparse.ArgumentParser(
        prog="state_db_remediate.py",
        description="Conservative Hermes state.db audit & remediation (Area 1).",
        epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--json", action="store_true", help="emit JSON")
        sp.add_argument("--report-file", help="also write the report to this path")

    a = sub.add_parser("audit", help="read-only inventory of state.db files")
    a.add_argument("--home", help="Hermes home to scan (default $HERMES_HOME or ~/.hermes)")
    a.add_argument("--db", action="append", help="audit a specific DB (repeatable)")
    a.add_argument("--follow-symlinks", action="store_true",
                   help="traverse symlinked dirs (reported either way)")
    add_common(a)
    a.set_defaults(func=cmd_audit)

    pl = sub.add_parser("plan", help="compute (dry) what a policy would delete + write policy.json")
    pl.add_argument("--home", help="Hermes home to scan")
    pl.add_argument("--db", help="plan a single DB")
    pl.add_argument("--retention-days", type=int, default=None,
                    help="age threshold (by last activity) for pruning (e.g. 30/60/90/180/365)")
    pl.add_argument("--prune-closed", default=None, help="yes|no: prune CLOSED sessions older than retention")
    pl.add_argument("--prune-unclosed", default=None, help="yes|no: also prune UNCLOSED sessions older than retention")
    pl.add_argument("--delete-compression-parents", default=None,
                    help="yes|no: delete compression parents that have a surviving child")
    pl.add_argument("--drop-trigram", default=None, help="yes|no: drop the trigram FTS index")
    pl.add_argument("--vacuum", default=None, help="yes|no: VACUUM after cleanup")
    pl.add_argument("--include-dormant", default=None, help="yes|no: include dormant profiles")
    pl.add_argument("--include-snapshots", default=None, help="yes|no: include backup snapshots")
    pl.add_argument("--protect-sources", default=None, help="comma-separated session sources to NEVER prune")
    pl.add_argument("--protect-recent-days", type=int, default=None,
                    help="never touch sessions active within N days (default 2)")
    pl.add_argument("--dormant-days", type=int, default=None, help="profile dormant if idle > N days (default 14)")
    pl.add_argument("--notes", default=None, help="free-text note saved in the policy")
    pl.add_argument("--out", help="write the resulting policy JSON to this path")
    pl.add_argument("--follow-symlinks", action="store_true")
    add_common(pl)
    pl.set_defaults(func=cmd_plan)

    s = sub.add_parser("simulate", help="run a policy on a COPY and verify (no original change)")
    s.add_argument("--db", required=True, help="database to simulate against (copied)")
    s.add_argument("--policy", required=True, help="policy JSON from `plan`")
    s.add_argument("--workdir", help="keep the cleaned copy here (else temp+deleted)")
    s.add_argument("--allow-snapshot", action="store_true", help="suppress the snapshot note")
    add_common(s)
    s.set_defaults(func=cmd_simulate)

    ap = sub.add_parser("apply", help="archive, clean-on-copy, verify, atomic swap (MUTATES)")
    ap.add_argument("--db", required=True, help="database to remediate")
    ap.add_argument("--policy", required=True, help="policy JSON from `plan`")
    ap.add_argument("--archive-dir", required=True,
                    help="directory to archive the original into (before changes)")
    ap.add_argument("--confirm-apply", action="store_true", help="REQUIRED. Without it, apply refuses.")
    ap.add_argument("--allow-busy", action="store_true",
                    help="skip the liveness guard (NOT recommended on live DBs)")
    ap.add_argument("--allow-snapshot", action="store_true",
                    help="REQUIRED to remediate a pre-update backup snapshot DB")
    add_common(ap)
    ap.set_defaults(func=cmd_apply)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
