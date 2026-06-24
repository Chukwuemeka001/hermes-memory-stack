#!/usr/bin/env python3
"""
semantic_index.py — Index Hermes session transcripts into ChromaDB for semantic search.

Reads sessions from every Hermes state.db (the default DB plus each profile DB),
embeds a short summary string per session with all-MiniLM-L6-v2 (384-dim), and
stores the vectors in a single persistent ChromaDB collection at
``~/.hermes/chroma/sessions``.

Companion to ``semantic_query.py`` (query side) and ``semantic_reindex.sh`` (cron
wrapper). The ``session_search`` tool shells out to ``semantic_query.py`` for
hybrid keyword+semantic retrieval; this script keeps the index fresh.

Design notes
------------
* READ-ONLY on state.db. Every SQLite connection is opened ``mode=ro`` via a
  file: URI — this process never writes to a state.db, never holds a write lock,
  and is safe to run against a live gateway's database.
* Incremental. Already-indexed session ids are skipped, so re-runs are cheap and
  idempotent. Pass ``--reset`` to drop and rebuild the collection.
* Heavy deps (chromadb, sentence_transformers) are imported lazily so importing
  this module is cheap. They live only under the system Python 3.14
  (``/opt/homebrew/lib/python3.14/site-packages``); run this with ``python3`` /
  ``python3.14``, NOT the agent venv (Python 3.11, which lacks them).
* Hidden session sources ("subagent", "tool") are excluded — they are not part
  of the user's browsable history, matching session_search's visibility rules.

Embedding text per session (kept focused on what the session is ABOUT):

    "{source} | {title} | {first_user_message[:500]}"

CLI
---
    python3 semantic_index.py                 # index all DBs (default + profiles)
    python3 semantic_index.py --db PATH        # index a single state.db
    python3 semantic_index.py --reset          # drop the collection, then rebuild
    python3 semantic_index.py --json           # machine-readable summary on stdout
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time

# Quiet, deterministic, offline-friendly. Set before any heavy import.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# ---- Shared contract with semantic_query.py (keep these in sync) ----
HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
CHROMA_PATH = os.path.join(HERMES_HOME, "chroma", "sessions")
COLLECTION_NAME = "sessions"
MODEL_NAME = "all-MiniLM-L6-v2"
HIDDEN_SOURCES = ("subagent", "tool")
EMBED_TEXT_MAX = 500

_chroma_client = None
_model = None


def _log(msg: str) -> None:
    """Progress to stderr so stdout stays clean for --json consumers."""
    print(msg, file=sys.stderr, flush=True)


def _get_chroma():
    global _chroma_client
    if _chroma_client is None:
        import chromadb
        from chromadb.config import Settings

        os.makedirs(CHROMA_PATH, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(
            path=CHROMA_PATH,
            settings=Settings(anonymized_telemetry=False),
        )
    return _chroma_client


def _get_collection():
    client = _get_chroma()
    # cosine space matches all-MiniLM-L6-v2 normalized embeddings
    return client.get_or_create_collection(
        COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
    )


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        t0 = time.time()
        _log(f"[semantic_index] loading model {MODEL_NAME} (first run downloads ~22MB) ...")
        _model = SentenceTransformer(MODEL_NAME)
        _log(f"[semantic_index] model ready in {time.time() - t0:.1f}s")
    return _model


def _profile_for_path(path: str) -> str:
    """Map a state.db path to its profile name (the default DB -> 'default')."""
    rp = os.path.realpath(path)
    if rp == os.path.realpath(os.path.join(HERMES_HOME, "state.db")):
        return "default"
    parent = os.path.basename(os.path.dirname(rp))  # .../profiles/<name>/state.db
    return parent or "unknown"


def discover_dbs():
    """Return [(db_path, profile_name), ...] for the default DB + every profile DB.

    db_path is realpath-normalized so it matches what SessionDB.db_path resolves
    to at query time (the per-db ``where`` filter keys off this exact string).
    """
    out = []
    seen = set()

    def add(path, profile):
        if not path or not os.path.exists(path):
            return
        rp = os.path.realpath(path)
        if rp in seen:
            return
        seen.add(rp)
        out.append((rp, profile))

    add(os.path.join(HERMES_HOME, "state.db"), "default")
    for p in sorted(glob.glob(os.path.join(HERMES_HOME, "profiles", "*", "state.db"))):
        profile = os.path.basename(os.path.dirname(p))
        add(p, profile)
    return out


def _open_ro(db_path: str) -> sqlite3.Connection:
    """Open a state.db strictly read-only (no write lock, WAL-readable)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set:
    """Column names of a table (for schema-version drift across older DBs)."""
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _fetch_sessions(conn: sqlite3.Connection):
    """Return session rows with their first real user message.

    Excludes hidden sources. The correlated subquery pulls the earliest
    non-empty user message — the best single signal for what a session is about.
    Older profile DBs lack the ``active`` column, so that predicate is only
    added when the column actually exists.
    """
    placeholders = ",".join("?" for _ in HIDDEN_SOURCES)
    active_pred = "AND m.active = 1" if "active" in _columns(conn, "messages") else ""
    sql = f"""
        SELECT
            s.id            AS id,
            s.source        AS source,
            s.title         AS title,
            s.started_at    AS started_at,
            s.model         AS model,
            s.parent_session_id AS parent_session_id,
            s.message_count AS message_count,
            (
                SELECT m.content
                FROM messages m
                WHERE m.session_id = s.id
                  AND m.role = 'user'
                  {active_pred}
                  AND m.content IS NOT NULL
                  AND TRIM(m.content) != ''
                ORDER BY m.timestamp ASC, m.id ASC
                LIMIT 1
            ) AS first_user_msg
        FROM sessions s
        WHERE s.source NOT IN ({placeholders})
    """
    return conn.execute(sql, HIDDEN_SOURCES).fetchall()


def _build_doc(row) -> str:
    """Embedding text: '{source} | {title} | {first_user_message[:500]}'.

    Empty parts are dropped so the model is not fed dangling separators.
    """
    parts = []
    src = (row["source"] or "").strip()
    if src:
        parts.append(src)
    title = (row["title"] or "").strip()
    if title:
        parts.append(title)
    msg = (row["first_user_msg"] or "").strip()
    if msg:
        parts.append(msg[:EMBED_TEXT_MAX])
    return " | ".join(parts)


def _build_metadata(row, db_path: str, profile: str) -> dict:
    """ChromaDB metadata must be flat scalars (str/int/float/bool), never None."""
    msg = (row["first_user_msg"] or "").strip()
    try:
        started = float(row["started_at"]) if row["started_at"] is not None else 0.0
    except (TypeError, ValueError):
        started = 0.0
    try:
        mcount = int(row["message_count"]) if row["message_count"] is not None else 0
    except (TypeError, ValueError):
        mcount = 0
    return {
        "session_id": str(row["id"] or ""),
        "source": str(row["source"] or ""),
        "title": str(row["title"] or ""),
        "started_at": started,
        "model": str(row["model"] or ""),
        "parent_session_id": str(row["parent_session_id"] or ""),
        "message_count": mcount,
        "db_path": db_path,
        "profile": profile,
        "preview": msg[:200],
    }


def index_db(db_path: str, profile: str, collection, existing_ids: set, batch_size: int = 64) -> dict:
    """Index one state.db. Returns a per-db summary dict."""
    summary = {"db_path": db_path, "profile": profile, "total": 0, "indexed": 0, "skipped": 0, "error": None}
    try:
        conn = _open_ro(db_path)
    except Exception as e:  # pragma: no cover - defensive
        summary["error"] = f"open failed: {e}"
        _log(f"[semantic_index] {profile}: cannot open ({e}) — skipping")
        return summary
    try:
        rows = _fetch_sessions(conn)
    except Exception as e:
        summary["error"] = f"query failed: {e}"
        _log(f"[semantic_index] {profile}: query failed ({e}) — skipping")
        conn.close()
        return summary
    conn.close()

    summary["total"] = len(rows)
    # Chroma id is composite ({profile}::{session_id}): session ids are NOT
    # globally unique across profiles (e.g. nclexclaude was branched from
    # lowcredit and shares 10 ids). Keying per (profile, session) keeps each
    # profile's copy distinct with its own db_path metadata, so the per-db
    # query filter resolves correctly. The bare session_id lives in metadata.
    to_index = [r for r in rows if f"{profile}::{r['id']}" not in existing_ids]
    summary["skipped"] = len(rows) - len(to_index)
    _log(
        f"[semantic_index] {profile}: {len(rows)} sessions, "
        f"{summary['skipped']} already indexed, {len(to_index)} to embed"
    )
    if not to_index:
        return summary

    model = _get_model()
    for i in range(0, len(to_index), batch_size):
        batch = to_index[i:i + batch_size]
        cids = [f"{profile}::{r['id']}" for r in batch]
        docs = [_build_doc(r) for r in batch]
        metas = [_build_metadata(r, db_path, profile) for r in batch]
        embeddings = model.encode(docs, show_progress_bar=False, normalize_embeddings=True).tolist()
        collection.add(ids=cids, embeddings=embeddings, documents=docs, metadatas=metas)
        existing_ids.update(cids)
        summary["indexed"] += len(batch)
        _log(f"[semantic_index]   {profile}: embedded {summary['indexed']}/{len(to_index)}")
    return summary


def index_all(db_path: str | None = None, reset: bool = False, batch_size: int = 64) -> dict:
    """Index every discovered DB (or a single one if db_path is given)."""
    t0 = time.time()
    client = _get_chroma()
    if reset:
        try:
            client.delete_collection(COLLECTION_NAME)
            _log("[semantic_index] --reset: dropped existing collection")
        except Exception:
            pass
    collection = _get_collection()

    # Snapshot already-indexed ids once; chromadb returns a flat id list.
    existing_ids = set()
    if collection.count() > 0:
        existing_ids = set(collection.get(include=[]).get("ids", []))

    if db_path:
        rp = os.path.realpath(os.path.expanduser(db_path))
        targets = [(rp, _profile_for_path(rp))]
    else:
        targets = discover_dbs()

    per_db = []
    for path, profile in targets:
        per_db.append(index_db(path, profile, collection, existing_ids, batch_size=batch_size))

    result = {
        "ok": True,
        "collection": COLLECTION_NAME,
        "chroma_path": CHROMA_PATH,
        "model": MODEL_NAME,
        "dbs": per_db,
        "total_sessions_seen": sum(d["total"] for d in per_db),
        "newly_indexed": sum(d["indexed"] for d in per_db),
        "already_indexed": sum(d["skipped"] for d in per_db),
        "collection_count": collection.count(),
        "elapsed_sec": round(time.time() - t0, 2),
    }
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Index Hermes sessions into ChromaDB for semantic search.")
    ap.add_argument("db", nargs="?", default=None, help="optional single state.db path (default: all DBs)")
    ap.add_argument("--db", dest="db_flag", default=None, help="single state.db path (alias for positional)")
    ap.add_argument("--reset", action="store_true", help="drop the collection and rebuild from scratch")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--json", action="store_true", help="emit JSON summary on stdout")
    args = ap.parse_args(argv)

    db_path = args.db_flag or args.db
    try:
        result = index_all(db_path=db_path, reset=args.reset, batch_size=args.batch_size)
    except Exception as e:
        err = {"ok": False, "error": str(e)}
        if args.json:
            print(json.dumps(err))
        else:
            _log(f"[semantic_index] FAILED: {e}")
        return 1

    if args.json:
        print(json.dumps(result))
    else:
        _log("")
        _log(f"[semantic_index] DONE in {result['elapsed_sec']}s")
        _log(f"  sessions seen:    {result['total_sessions_seen']}")
        _log(f"  newly indexed:    {result['newly_indexed']}")
        _log(f"  already indexed:  {result['already_indexed']}")
        _log(f"  collection total: {result['collection_count']}")
        for d in result["dbs"]:
            tag = f" (error: {d['error']})" if d["error"] else ""
            _log(f"    - {d['profile']:<14} total={d['total']:<4} new={d['indexed']:<4} skip={d['skipped']:<4}{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
